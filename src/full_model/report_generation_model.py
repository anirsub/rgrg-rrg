from typing import List, Dict, Optional, Tuple

import torch
import torch.nn as nn

from src.binary_classifier.binary_classifier_region_selection import BinaryClassifierRegionSelection
from src.object_detector.object_detector import ObjectDetector
from src.language_model.language_model import LanguageModel


class ReportGenerationModel(nn.Module):
    """
    Full model consisting of:
        - object detector encoder
        - binary classifier for selecting regions for sentence genneration
        - binary classifier for detecting if a region is abnormal or normal (to encode this information in the region feature vectors)
        - language model decoder
    """

    def __init__(self, pretrain_without_lm_model=False):
        super().__init__()
        self.pretrain_without_lm_model = pretrain_without_lm_model

        self.object_detector = ObjectDetector(return_feature_vectors=True)
        path_to_best_object_detector_weights = "/u/home/tanida/runs/object_detector/run_12/weights/val_loss_13.067_epoch_8.pth"
        checkpoint = torch.load(path_to_best_object_detector_weights, map_location=torch.device("cpu"))

        checkpoint["rpn.head.conv.weight"] = checkpoint.pop("rpn.head.conv.0.0.weight")
        checkpoint["rpn.head.conv.bias"] = checkpoint.pop("rpn.head.conv.0.0.bias")

        self.object_detector.load_state_dict(checkpoint)

        self.binary_classifier_region_selection = BinaryClassifierRegionSelection()

        self.language_model = LanguageModel()

    def forward(
        self,
        images: torch.FloatTensor,  # images is of shape [batch_size x 1 x 512 x 512] (whole gray-scale images of size 512 x 512)
        image_targets: List[Dict],  # contains a dict for every image with keys "boxes" and "labels"
        input_ids: torch.LongTensor,  # shape [(batch_size * 29) x seq_len], 1 sentence for every region for every image (sentence can be empty, i.e. "")
        attention_mask: torch.FloatTensor,  # shape [(batch_size * 29) x seq_len]
        region_has_sentence: torch.BoolTensor,  # shape [batch_size x 29], ground truth boolean mask that indicates if a region has a sentence or not
        region_is_abnormal: torch.BoolTensor,  # shape [batch_size x 29], ground truth boolean mask that indicates if a region has is abnormal or not
        return_loss: bool = True,
        past_key_values: Optional[Tuple[Tuple[torch.Tensor]]] = None,
        position_ids: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = False,
    ):
        """
        Forward method is used for training and evaluation of model.
        Generate method is used for inference.
        """
        if self.training:
            # top_region_features of shape [batch_size x 29 x 1024] (i.e. 1 feature vector for every region for every image in batch)
            # class_detected is a boolean tensor of shape [batch_size x 29]. Its value is True for a class if the object detector detected the class/region in the image
            obj_detector_loss_dict, top_region_features, class_detected = self.object_detector(images, image_targets)

            # delete tensors that we don't need anymore to free up GPU resources
            del images
            del image_targets

            # during training, only get the two losses for the two binary classifiers

            classifier_loss_region_selection = self.binary_classifier_region_selection(
                top_region_features, class_detected, return_loss=True, region_has_sentence=region_has_sentence
            )

            if self.pretrain_without_lm_model:
                return obj_detector_loss_dict, classifier_loss_region_selection

            # to train the decoder, we want to use only the top region features (and corresponding input_ids, attention_mask)
            # of regions that were both detected by the object detector and have a sentence as the ground truth
            # this is done under the assumption that at inference time, the binary classifier for region selection will do an adequate job
            # at selecting those regions that need a sentence to be generated by itself
            valid_input_ids, valid_attention_mask, valid_region_features = self.get_valid_decoder_input_for_training(
                class_detected, region_has_sentence, input_ids, attention_mask, top_region_features
            )

            del top_region_features
            del region_has_sentence
            del region_is_abnormal
            del class_detected
            del input_ids
            del attention_mask

        else:
            # during evaluation, also return detections (i.e. detected bboxes)
            obj_detector_loss_dict, detections, top_region_features, class_detected = self.object_detector(images, image_targets)

            del images
            del image_targets

            # during evaluation, for the binary classifier for region selection, get the loss, the regions that were selected by the classifier
            # (and that were also detected) and the corresponding region features (selected_region_features)
            # this is done to evaluate the decoder under "real-word" conditions, i.e. the binary classifier decides which regions get a sentence
            classifier_loss_region_selection, selected_regions, selected_region_features = self.binary_classifier_region_selection(
                top_region_features, class_detected, return_loss=True, region_has_sentence=region_has_sentence
            )

            if self.pretrain_without_lm_model:
                return obj_detector_loss_dict, classifier_loss_region_selection, detections, class_detected, selected_regions

            del top_region_features
            del region_has_sentence
            del region_is_abnormal

            # use the selected_regions mask to filter the inputs_ids and attention_mask to those that correspond to regions that were selected
            valid_input_ids, valid_attention_mask = self.get_valid_decoder_input_for_evaluation(selected_regions, input_ids, attention_mask)
            valid_region_features = selected_region_features

            del input_ids
            del attention_mask

        # valid_input_ids can be empty if during:
        # training:
        #   - the regions that have a gt sentence (specified by region_has_sentence) were all not detected (specified by class_detected).
        #   This can happend if e.g. a lateral chest x-ray was erroneously included in the dataset (and hence the object detector not detecting
        #   any regions, since it was trained on frontal chest x-rays)
        # evaluation:
        #   - no regions were selected by the binary classifier (specified by selected_regions)
        #   - the regions that were selected by the binary classifier for region selection were all not detected (also specified by selected_regions,
        #   since class_detected is encoded in selected_regions). Again, the reason might be a bad input image
        #
        # empty valid_input_ids (and thus empty valid_attention_mask, valid_region_features) will throw an exception in the language model,
        # which is why we have to return early
        if valid_input_ids.shape[0] == 0:
            return -1

        language_model_loss = self.language_model(
            valid_input_ids,
            valid_attention_mask,
            valid_region_features,
            return_loss,
            past_key_values,
            position_ids,
            use_cache,
        )

        del valid_input_ids
        del valid_attention_mask
        del valid_region_features

        if self.training:
            return obj_detector_loss_dict, classifier_loss_region_selection, language_model_loss
        else:
            # class_detected needed to evaluate how good the object detector is at detecting the different regions during evaluation
            # detections and class_detected needed to compute IoU of object detector during evaluation
            # selected_regions needed to evaluate binary classifier for region selection during evaluation and
            # to map each generated sentence to its corresponding region (for example for plotting)
            # predicted_abnormal_regions needed to evalute the binary classifier for normal/abnormal detection
            return (
                obj_detector_loss_dict,
                classifier_loss_region_selection,
                language_model_loss,
                detections,
                class_detected,
                selected_regions
            )

    def get_valid_decoder_input_for_training(
        self,
        class_detected,  # shape [batch_size x 29]
        region_has_sentence,  # shape [batch_size x 29]
        input_ids,  # shape [(batch_size * 29) x seq_len]
        attention_mask,  # shape [(batch_size * 29) x seq_len]
        region_features,  # shape [batch_size x 29 x 1024]
    ):
        """
        We want to train the decoder only on region features (and corresponding input_ids/attention_mask) whose corresponding sentences are non-empty and
        that were detected by the object detector.
        """
        # valid is of shape [batch_size x 29]
        valid = torch.logical_and(class_detected, region_has_sentence)

        # reshape to [(batch_size * 29)], such that we can apply the mask to input_ids and attention_mask
        valid_reshaped = valid.reshape(-1)

        valid_input_ids = input_ids[valid_reshaped]  # of shape [num_detected_regions_with_non_empty_gt_phrase_in_batch x seq_len]
        valid_attention_mask = attention_mask[valid_reshaped]  # of shape [num_detected_regions_with_non_empty_gt_phrase_in_batch x seq_len]
        valid_region_features = region_features[valid]  # of shape [num_detected_regions_with_non_empty_gt_phrase_in_batch x 1024]

        return valid_input_ids, valid_attention_mask, valid_region_features

    def get_valid_decoder_input_for_evaluation(
        self,
        selected_regions,  # shape [batch_size x 29]
        input_ids,  # shape [(batch_size * 29) x seq_len]
        attention_mask  # shape [(batch_size * 29) x seq_len]
    ):
        """
        For evaluation, we want to evaluate the decoder on the top_region_features selected by the classifier to get a sentence generated.
        We also have to get the corresponding input_ids and attention_mask accordingly.
        """
        # reshape to [(batch_size * 29)]
        selected_regions = selected_regions.reshape(-1)

        valid_input_ids = input_ids[selected_regions]  # of shape [num_regions_selected_in_batch x seq_len]
        valid_attention_mask = attention_mask[selected_regions]  # of shape [num_regions_selected_in_batch x seq_len]

        return valid_input_ids, valid_attention_mask

    @torch.no_grad()
    def generate(
        self,
        images: torch.FloatTensor,  # images is of shape [batch_size x 1 x 512 x 512] (whole gray-scale images of size 512 x 512)
        max_length: int = None,
        num_beams: int = 1,
        num_beam_groups: int = 1,
        do_sample: bool = False,
        num_return_sequences: int = 1,
        early_stopping: bool = False,
    ):
        """
        In inference mode, we usually input 1 image (with 29 regions) at a time.

        The object detector first finds the region features for all 29 regions.

        The binary classifier takes the region_features of shape [batch_size=1, 29, 1024] and returns:
            - selected_region_features: shape [num_regions_selected_in_batch, 1024],
            all region_features which were selected by the classifier to get a sentence generated (and which were also detected by the object detector)

            - selected_regions: shape [batch_size x 29], boolean matrix that indicates which regions were selected to get a sentences generated
            (these regions must also have been detected by the object detector).
            This is needed in case we want to find the corresponding reference sentences to compute scores for metrics such as BertScore or BLEU.

        The decoder then takes the selected_region_features and generates output ids for the batch.
        These output ids can then be decoded by the tokenizer to get the generated sentences.

        We also return selected_regions, such that we can map each generated sentence to a selected region.
        We also return detections, such that we can map each generated sentence to a bounding box.
        We also return class_detected to know which regions were not detected by the object detector (can be plotted).
        """
        # top_region_features of shape [batch_size x 29 x 1024]
        _, detections, top_region_features, class_detected = self.object_detector(images)

        del images

        # selected_region_features is of shape [num_regions_selected_in_batch x 1024]
        # selected_regions is of shape [batch_size x 29] and is True for regions that should get a sentence
        # (it has exactly num_regions_selected_in_batch True values)
        selected_regions, selected_region_features = self.binary_classifier_region_selection(
            top_region_features, class_detected, return_loss=False
        )

        del top_region_features

        # selected_region_features can be empty if no region was both detected by the object detector and selected
        # by the binary classifier to get a sentence generated. This can happen especially early on in training
        # Since this would throw an exception in the language model, we return early
        if selected_region_features.shape[0] == 0:
            return -1

        # output_ids of shape (num_regions_selected_in_batch x longest_generated_sequence_length)
        output_ids = self.language_model.generate(
            selected_region_features,
            max_length,
            num_beams,
            num_beam_groups,
            do_sample,
            num_return_sequences,
            early_stopping,
        )

        del selected_region_features

        return output_ids, selected_regions, detections, class_detected
