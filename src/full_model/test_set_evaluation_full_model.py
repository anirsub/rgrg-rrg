from ast import literal_eval
import logging
import os
import random

import albumentations as A
from albumentations.pytorch import ToTensorV2
import cv2
from datasets import Dataset
import numpy as np
import pandas as pd
import spacy
import torch
from torch.utils.data import DataLoader
import torchmetrics
from transformers import GPT2Tokenizer
from tqdm import tqdm

from src.dataset.constants import ANATOMICAL_REGIONS
from src.full_model.custom_collator import CustomCollator
from src.full_model.custom_dataset import CustomDataset
from src.full_model.evaluate_full_model.evaluate_model import (
    update_object_detector_metrics,
    update_region_abnormal_metrics,
    update_region_selection_metrics,
)
from src.full_model.evaluate_full_model.evaluate_language_model import (
    get_ref_sentences_for_selected_regions,
    get_sents_for_normal_abnormal_selected_regions,
    get_generated_and_reference_reports,
    compute_language_model_scores
)
from src.full_model.report_generation_model import ReportGenerationModel
from src.path_datasets_and_weights import path_full_dataset

RUN = 43
CHECKPOINT = "checkpoint_val_loss_23.803_overall_steps_180901"
BERTSCORE_SIMILARITY_THRESHOLD = 0.9
IMAGE_INPUT_SIZE = 512
BATCH_SIZE = 4
NUM_WORKERS = 10
NUM_BEAMS = 4
MAX_NUM_TOKENS_GENERATE = 300
NUM_BATCHES_OF_GENERATED_SENTENCES_TO_SAVE_TO_FILE = 100
NUM_BATCHES_OF_GENERATED_REPORTS_TO_SAVE_TO_FILE = 100
NUM_BATCHES_TO_PROCESS_FOR_LANGUAGE_MODEL_EVALUATION = 500
NUM_IMAGES_TO_USE_IN_TEST_SET = 5000

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

logging.basicConfig(level=logging.INFO, format="[%(levelname)s]: %(message)s")
log = logging.getLogger(__name__)

# set the seed value for reproducibility
seed_val = 42

random.seed(seed_val)
np.random.seed(seed_val)
torch.manual_seed(seed_val)
torch.cuda.manual_seed_all(seed_val)

path_to_test_mimic_reports_folder = "/u/home/tanida/datasets/mimic-cxr-reports/test_2000_reports"
path_to_test_mimic_reports_folder_findings_only = "/u/home/tanida/datasets/mimic-cxr-reports/test_2000_reports_findings_only"

final_scores_txt_file = os.path.join("/u/home/tanida/region-guided-chest-x-ray-report-generation/src/", "final_scores.txt")


def write_all_scores_to_file(
    obj_detector_scores,
    region_selection_scores,
    region_abnormal_scores,
    language_model_scores,
):
    def write_obj_detector_scores():
        with open(final_scores_txt_file, "a") as f:
            f.write(f"avg_num_detected_regions_per_image: {obj_detector_scores['avg_num_detected_regions_per_image']}\n")
            f.write(f"avg_iou: {obj_detector_scores['avg_iou']}\n")

        # replace white space by underscore for each region name (i.e. "right upper lung" -> "right_upper_lung")
        anatomical_regions = ["_".join(region.split()) for region in ANATOMICAL_REGIONS]
        avg_detections_per_region = obj_detector_scores["avg_detections_per_region"]
        avg_iou_per_region = obj_detector_scores["avg_iou_per_region"]

        for region_, avg_detections_region in zip(anatomical_regions, avg_detections_per_region):
            with open(final_scores_txt_file, "a") as f:
                f.write(f"num_detected_{region_}: {avg_detections_region}\n")

        for region_, avg_iou_region in zip(anatomical_regions, avg_iou_per_region):
            with open(final_scores_txt_file, "a") as f:
                f.write(f"iou_{region_}: {avg_iou_region}\n")

    def write_region_selection_scores():
        for subset in region_selection_scores:
            for metric, score in region_selection_scores[subset].items():
                with open(final_scores_txt_file, "a") as f:
                    f.write(f"region_select_{subset}_{metric}: {score}\n")

    def write_region_abnormal_scores():
        for metric, score in region_abnormal_scores.items():
            with open(final_scores_txt_file, "a") as f:
                f.write(f"region_abnormal_{metric}: {score}\n")

    def write_clinical_efficacy_scores(subset, ce_score_dict):
        """
        ce_score_dict is of the structure:

        {
            precision: ...,
            recall: ...,
            f1: ...,
            acc: ...,
            condition_1 : {
                precision: ...,
                recall: ...,
                f1: ...,
                acc: ...,
            },
            condition_2 : {
                precision: ...,
                recall: ...,
                f1: ...,
                acc: ...,
            },
            ...,
            condition_5 : {
                precision: ...,
                recall: ...,
                f1: ...,
                acc: ...,
            }
        }

        where the "..." after the 4 metrics are the corresponding scores,
        and condition_* are from the 5 conditions we evaluate on (i.e. "Cardiomegaly", "Edema", "Consolidation", "Atelectasis", "Pleural Effusion")
        """
        metrics = {"precision", "recall", "f1", "acc"}

        for k, v in ce_score_dict.items():
            if k in metrics:
                with open(final_scores_txt_file, "a") as f:
                    f.write(f"language_model_{subset}_CE_{k}: {v}\n")
            else:
                # k is a condition
                condition_name = "_".join(k.lower().split())
                for metric, score in ce_score_dict[k].items():
                    with open(final_scores_txt_file, "a") as f:
                        f.write(f"language_model_{subset}_CE_{condition_name}_{metric}: {score}\n")

    def write_language_model_scores():
        for subset in language_model_scores:
            for metric, score in language_model_scores[subset].items():
                with open(final_scores_txt_file, "a") as f:
                    if metric == "CE":
                        ce_score_dict = language_model_scores[subset]["CE"]
                        write_clinical_efficacy_scores(subset, ce_score_dict)
                    else:
                        f.write(f"language_model_{subset}_{metric}: {score}\n")

    with open(final_scores_txt_file, "a") as f:
        f.write(f"Run: {RUN}\n")
        f.write(f"Checkpoint: {CHECKPOINT}\n")
        f.write(f"BertScore: {BERTSCORE_SIMILARITY_THRESHOLD}\n")

    write_obj_detector_scores()
    write_region_selection_scores()
    write_region_abnormal_scores()
    write_language_model_scores()


def write_sentences_and_reports_to_file(gen_and_ref_sentences, gen_and_ref_reports):
    def write_sentences(generated_sentences, generated_sentences_abnormal_regions, reference_sentences, reference_sentences_abnormal_regions):
        txt_file_name = os.path.join("/u/home/tanida/region-guided-chest-x-ray-report-generation/src/", "generated_sentences.txt")
        txt_file_name_abnormal = os.path.join("/u/home/tanida/region-guided-chest-x-ray-report-generation/src/", "generated_abnormal_sentences.txt")

        with open(txt_file_name, "w") as f:
            for gen_sent, ref_sent in zip(generated_sentences, reference_sentences):
                f.write(f"Generated sentence: {gen_sent}\n")
                # the hash symbol symbolizes an empty reference sentence, and thus can be replaced by '' when writing to file
                f.write(f"Reference sentence: {ref_sent if ref_sent != '#' else ''}\n\n")

        with open(txt_file_name_abnormal, "w") as f:
            for gen_sent, ref_sent in zip(generated_sentences_abnormal_regions, reference_sentences_abnormal_regions):
                f.write(f"Generated sentence: {gen_sent}\n")
                f.write(f"Reference sentence: {ref_sent if ref_sent != '#' else ''}\n\n")

    def write_reports(generated_reports, reference_reports, reference_reports_mimic, reference_reports_mimic_findings_only, removed_similar_generated_sentences):
        txt_file_name = os.path.join("/u/home/tanida/region-guided-chest-x-ray-report-generation/src/", "generated_reports.txt")

        with open(txt_file_name, "w") as f:
            for gen_report, ref_report, ref_report_mimic, ref_report_mimic_findings_only, removed_similar_gen_sents in zip(
                generated_reports, reference_reports, reference_reports_mimic, reference_reports_mimic_findings_only, removed_similar_generated_sentences
            ):
                f.write(f"Generated report: {gen_report}\n\n")
                f.write(f"Reference report: {ref_report}\n\n")
                f.write(f"Ref report mimic: {ref_report_mimic}\n\n")
                f.write(f"Ref report mimic findings only: {ref_report_mimic_findings_only if ref_report_mimic_findings_only is not None else '[EMPTY]'}\n\n")
                f.write("Generated sentences that were removed:\n")
                for gen_sent, list_similar_gen_sents in removed_similar_gen_sents.items():
                    f.write(f"\t{gen_sent} == {list_similar_gen_sents}\n")
                f.write("\n")
                f.write("=" * 30)
                f.write("\n\n")

    # all below are list of str
    generated_sentences = gen_and_ref_sentences["generated_sentences"][:NUM_BATCHES_OF_GENERATED_SENTENCES_TO_SAVE_TO_FILE]
    generated_sentences_abnormal_regions = gen_and_ref_sentences["generated_sentences_abnormal_selected_regions"][:NUM_BATCHES_OF_GENERATED_SENTENCES_TO_SAVE_TO_FILE]
    reference_sentences = gen_and_ref_sentences["reference_sentences"][:NUM_BATCHES_OF_GENERATED_SENTENCES_TO_SAVE_TO_FILE]
    reference_sentences_abnormal_regions = gen_and_ref_sentences["reference_sentences_abnormal_selected_regions"][:NUM_BATCHES_OF_GENERATED_SENTENCES_TO_SAVE_TO_FILE]

    write_sentences(generated_sentences, generated_sentences_abnormal_regions, reference_sentences, reference_sentences_abnormal_regions)

    # all below are list of str except removed_similar_generated_sentences which is a list of dict
    generated_reports = gen_and_ref_reports["generated_reports"][:NUM_BATCHES_OF_GENERATED_REPORTS_TO_SAVE_TO_FILE]
    reference_reports = gen_and_ref_reports["reference_reports"][:NUM_BATCHES_OF_GENERATED_REPORTS_TO_SAVE_TO_FILE]
    reference_reports_mimic = gen_and_ref_reports["reference_reports_mimic"][:NUM_BATCHES_OF_GENERATED_REPORTS_TO_SAVE_TO_FILE]
    reference_reports_mimic_findings_only = gen_and_ref_reports["reference_reports_mimic_findings_only"][:NUM_BATCHES_OF_GENERATED_REPORTS_TO_SAVE_TO_FILE]
    removed_similar_generated_sentences = gen_and_ref_reports["removed_similar_generated_sentences"][:NUM_BATCHES_OF_GENERATED_REPORTS_TO_SAVE_TO_FILE]

    write_reports(generated_reports, reference_reports, reference_reports_mimic, reference_reports_mimic_findings_only, removed_similar_generated_sentences)


def get_reference_reports_mimic(study_ids) -> dict[str, list]:
    """
    The folder "/u/home/tanida/datasets/mimic-cxr-reports/test_2000_reports" (specified by path_to_test_mimic_reports_folder)
    contains 2000 mimic-cxr reports that correspond to the first 2000 images in the test set.

    The number 2000 was chosen because we generate 2000 reports for the first 2000 images in the test set during each test set evaluation,
    (2000 = NUM_BATCHES_TO_PROCESS_FOR_LANGUAGE_MODEL_EVALUATION * BATCH_SIZE = 500 * 4)
    since generating a report for every image in the test set would take too long.

    The original mimic-cxr reports were processed (see dataset/convert_mimic_cxr_report_to_single_string.py) from txt files that
    contained multiple lines (containing irrelevant information) to txt files that only contain a single line (containing the information
    from the findings and impression sections of the original report).
    """
    reference_reports_mimic = {
        "report_mimic": [],
        "report_mimic_findings_only": []
    }

    for study_id in study_ids:
        study_txt_file_path = os.path.join(path_to_test_mimic_reports_folder, f"s{study_id}.txt")
        with open(study_txt_file_path) as f:
            report = f.readline()
            reference_reports_mimic["report_mimic"].append(report)

        study_txt_file_path = os.path.join(path_to_test_mimic_reports_folder_findings_only, f"s{study_id}.txt")
        if os.path.exists(study_txt_file_path):
            with open(study_txt_file_path) as f:
                report = f.readline()
                reference_reports_mimic["report_mimic_findings_only"].append(report)
        else:
            reference_reports_mimic["report_mimic_findings_only"].append(None)

    return reference_reports_mimic


def evaluate_language_model(model, test_loader, tokenizer):
    gen_and_ref_sentences = {
        "generated_sentences": [],
        "generated_sentences_normal_selected_regions": [],
        "generated_sentences_abnormal_selected_regions": [],
        "reference_sentences": [],
        "reference_sentences_normal_selected_regions": [],
        "reference_sentences_abnormal_selected_regions": [],
    }

    gen_and_ref_reports = {
        "generated_reports": [],
        "removed_similar_generated_sentences": [],
        "reference_reports": [],
        "reference_reports_mimic": [],
        "reference_reports_mimic_findings_only": []
    }

    # used in function get_generated_and_reference_reports
    sentence_tokenizer = spacy.load("en_core_web_trf")

    with torch.no_grad():
        for num_batch, batch in tqdm(enumerate(test_loader), total=NUM_BATCHES_TO_PROCESS_FOR_LANGUAGE_MODEL_EVALUATION):
            # since generating sentences takes some time, we limit the number of batches used to compute bleu/rouge-l/meteor
            if num_batch >= NUM_BATCHES_TO_PROCESS_FOR_LANGUAGE_MODEL_EVALUATION:
                break

            images = batch["images"]  # shape [batch_size x 1 x 512 x 512]
            region_is_abnormal = batch["region_is_abnormal"].numpy()  # boolean array of shape [batch_size x 29]

            # List[List[str]] that holds the reference phrases. The inner list holds all reference phrases of a single image
            reference_sentences = batch["reference_sentences"]

            # List[str] that holds the study ids for the images in the batch. These are used to retrieve the corresponding
            # MIMIC-CXR reports from a separate folder
            study_ids = batch["study_ids"]

            with torch.autocast(device_type='cuda', dtype=torch.float16):
                output = model.generate(
                    images.to(device, non_blocking=True),
                    max_length=MAX_NUM_TOKENS_GENERATE,
                    num_beams=NUM_BEAMS,
                    early_stopping=True,
                )

            beam_search_output, selected_regions, _, _ = output
            selected_regions = selected_regions.detach().cpu().numpy()

            # generated_sentences_for_selected_regions is a List[str] of length "num_regions_selected_in_batch"
            generated_sentences_for_selected_regions = tokenizer.batch_decode(
                beam_search_output, skip_special_tokens=True, clean_up_tokenization_spaces=True
            )

            # filter reference_sentences to those that correspond to the generated_sentences for the selected regions.
            # reference_sentences_for_selected_regions will therefore be a List[str] of length "num_regions_selected_in_batch"
            # (i.e. same length as generated_sentences_for_selected_regions)
            reference_sentences_for_selected_regions = get_ref_sentences_for_selected_regions(
                reference_sentences, selected_regions
            )

            (
                gen_sents_for_normal_selected_regions,
                gen_sents_for_abnormal_selected_regions,
                ref_sents_for_normal_selected_regions,
                ref_sents_for_abnormal_selected_regions,
            ) = get_sents_for_normal_abnormal_selected_regions(region_is_abnormal, selected_regions, generated_sentences_for_selected_regions, reference_sentences_for_selected_regions)

            (
                generated_reports,
                reference_reports,
                removed_similar_generated_sentences,
            ) = get_generated_and_reference_reports(
                generated_sentences_for_selected_regions, reference_sentences, selected_regions, sentence_tokenizer, BERTSCORE_SIMILARITY_THRESHOLD
            )

            reference_reports_mimic = get_reference_reports_mimic(study_ids)

            gen_and_ref_sentences["generated_sentences"].extend(generated_sentences_for_selected_regions)
            gen_and_ref_sentences["generated_sentences_normal_selected_regions"].extend(gen_sents_for_normal_selected_regions)
            gen_and_ref_sentences["generated_sentences_abnormal_selected_regions"].extend(gen_sents_for_abnormal_selected_regions)
            gen_and_ref_sentences["reference_sentences"].extend(reference_sentences_for_selected_regions)
            gen_and_ref_sentences["reference_sentences_normal_selected_regions"].extend(ref_sents_for_normal_selected_regions)
            gen_and_ref_sentences["reference_sentences_abnormal_selected_regions"].extend(ref_sents_for_abnormal_selected_regions)
            gen_and_ref_reports["generated_reports"].extend(generated_reports)
            gen_and_ref_reports["reference_reports"].extend(reference_reports)
            gen_and_ref_reports["reference_reports_mimic"].extend(reference_reports_mimic["report_mimic"])
            gen_and_ref_reports["reference_reports_mimic_findings_only"].extend(reference_reports_mimic["report_mimic_findings_only"])
            gen_and_ref_reports["removed_similar_generated_sentences"].extend(removed_similar_generated_sentences)

    write_sentences_and_reports_to_file(gen_and_ref_sentences, gen_and_ref_reports)

    with open(final_scores_txt_file, "a") as f:
        f.write(f"Num generated reports: {len(gen_and_ref_reports['generated_reports'])}\n")
        f.write(f"Num reference reports findings only: {len([report for report in gen_and_ref_reports['reference_reports_mimic_findings_only'] if report is not None])}\n")

    language_model_scores = compute_language_model_scores(gen_and_ref_sentences, gen_and_ref_reports)

    return language_model_scores


def get_metric_scores(model, test_loader):
    obj_detector_scores = {}
    obj_detector_scores["sum_intersection_area_per_region"] = torch.zeros(29, device=device)
    obj_detector_scores["sum_union_area_per_region"] = torch.zeros(29, device=device)
    obj_detector_scores["sum_region_detected"] = torch.zeros(29, device=device)

    region_selection_scores = {}
    for subset in ["all", "normal", "abnormal"]:
        region_selection_scores[subset] = {
            "precision": torchmetrics.Precision(num_classes=2, average=None).to(device),
            "recall": torchmetrics.Recall(num_classes=2, average=None).to(device),
            "f1": torchmetrics.F1Score(num_classes=2, average=None).to(device),
        }

    region_abnormal_scores = {
        "precision": torchmetrics.Precision(num_classes=2, average=None).to(device),
        "recall": torchmetrics.Recall(num_classes=2, average=None).to(device),
        "f1": torchmetrics.F1Score(num_classes=2, average=None).to(device),
    }

    # to recover from out of memory error if a batch has a sequence that is too long
    oom = False

    num_images = 0

    with torch.no_grad():
        for num_batch, batch in tqdm(enumerate(test_loader)):
            images = batch["images"]
            image_targets = batch["image_targets"]
            region_has_sentence = batch["region_has_sentence"]
            region_is_abnormal = batch["region_is_abnormal"]
            input_ids = batch["input_ids"]
            attention_mask = batch["attention_mask"]

            batch_size = images.size(0)
            num_images += batch_size

            images = images.to(device, non_blocking=True)
            image_targets = [{k: v.to(device, non_blocking=True) for k, v in t.items()} for t in image_targets]
            region_has_sentence = region_has_sentence.to(device, non_blocking=True)
            region_is_abnormal = region_is_abnormal.to(device, non_blocking=True)
            input_ids = input_ids.to(device, non_blocking=True)
            attention_mask = attention_mask.to(device, non_blocking=True)

            try:
                with torch.autocast(device_type="cuda", dtype=torch.float16):
                    output = model(
                        images, image_targets, input_ids, attention_mask, region_has_sentence, region_is_abnormal
                    )
            except RuntimeError as e:  # out of memory error
                if "out of memory" in str(e):
                    oom = True

                    print(f"OOM at batch number {num_batch}.\n")
                    print(f"Error message: {str(e)}\n\n")

                    raise e

            if oom:
                # free up memory
                torch.cuda.empty_cache()
                oom = False

                num_images -= batch_size

                continue

            # output == -1 if the region features that would have been passed into the language model were empty (see forward method for more details)
            if output == -1:
                print(f"Empty region features before language model at batch number {num_batch}.\n\n")

                num_images -= batch_size
                continue

            (
                _,
                _,
                _,
                _,
                detections,
                class_detected,  # bool tensor of shape [batch_size x 29]
                selected_regions,  # bool tensor of shape [batch_size x 29]
                predicted_abnormal_regions,  # bool tensor of shape [batch_size x 29]
            ) = output

            # update scores for object detector metrics
            update_object_detector_metrics(obj_detector_scores, detections, image_targets, class_detected)

            # update scores for region selection metrics
            update_region_selection_metrics(
                region_selection_scores, selected_regions, region_has_sentence, region_is_abnormal
            )

            # update scores for region abnormal detection metrics
            update_region_abnormal_metrics(
                region_abnormal_scores, predicted_abnormal_regions, region_is_abnormal, class_detected
            )

    # compute object detector scores
    sum_intersection = obj_detector_scores["sum_intersection_area_per_region"]
    sum_union = obj_detector_scores["sum_union_area_per_region"]
    obj_detector_scores["avg_iou"] = (torch.sum(sum_intersection) / torch.sum(sum_union)).item()
    obj_detector_scores["avg_iou_per_region"] = (sum_intersection / sum_union).tolist()

    sum_region_detected = obj_detector_scores["sum_region_detected"]
    obj_detector_scores["avg_num_detected_regions_per_image"] = torch.sum(sum_region_detected / num_images).item()
    obj_detector_scores["avg_detections_per_region"] = (sum_region_detected / num_images).tolist()

    # compute the "micro" average scores for region_selection_scores
    for subset in region_selection_scores:
        for metric, score in region_selection_scores[subset].items():
            region_selection_scores[subset][metric] = score.compute()[
                1
            ].item()  # only report results for the positive class (hence [1])

    # compute the "micro" average scores for region_abnormal_scores
    for metric, score in region_abnormal_scores.items():
        region_abnormal_scores[metric] = score.compute()[1].item()

    return obj_detector_scores, region_selection_scores, region_abnormal_scores


def evaluate_model(model, test_loader, tokenizer):
    obj_detector_scores, region_selection_scores, region_abnormal_scores = get_metric_scores(
        model, test_loader
    )

    language_model_scores = evaluate_language_model(model, test_loader, tokenizer)

    write_all_scores_to_file(
        obj_detector_scores,
        region_selection_scores,
        region_abnormal_scores,
        language_model_scores,
    )


def get_data_loader(tokenizer, test_dataset_complete):
    custom_collate_test = CustomCollator(
        tokenizer=tokenizer, is_val_or_test=True, pretrain_without_lm_model=False
    )

    test_loader = DataLoader(
        test_dataset_complete,
        collate_fn=custom_collate_test,
        batch_size=BATCH_SIZE,
        shuffle=False,
        num_workers=0,
        pin_memory=True,
    )
    return test_loader


def get_transforms():
    # see compute_mean_std_dataset.py in src/dataset
    mean = 0.471
    std = 0.302

    # don't apply data augmentations to test set
    test_transforms = A.Compose(
        [
            A.LongestMaxSize(max_size=IMAGE_INPUT_SIZE, interpolation=cv2.INTER_AREA),
            A.PadIfNeeded(min_height=IMAGE_INPUT_SIZE, min_width=IMAGE_INPUT_SIZE, border_mode=cv2.BORDER_CONSTANT),
            A.Normalize(mean=mean, std=std),
            ToTensorV2(),
        ],
        bbox_params=A.BboxParams(format="pascal_voc", label_fields=["class_labels"]),
    )

    return test_transforms


def get_tokenized_dataset(tokenizer, raw_test_dataset):
    def tokenize_function(example):
        phrases = example["bbox_phrases"]  # List[str]
        bos_token = "<|endoftext|>"  # note: in the GPT2 tokenizer, bos_token = eos_token = "<|endoftext|>"
        eos_token = "<|endoftext|>"

        phrases_with_special_tokens = [bos_token + phrase + eos_token for phrase in phrases]

        # the tokenizer will return input_ids of type List[List[int]] and attention_mask of type List[List[int]]
        return tokenizer(phrases_with_special_tokens, truncation=True, max_length=1024)

    tokenized_test_dataset = raw_test_dataset.map(tokenize_function)

    # tokenized datasets will consist of the columns
    #   - study_id
    #   - mimic_image_file_path
    #   - bbox_coordinates (List[List[int]])
    #   - bbox_labels (List[int])
    #   - bbox_phrases (List[str])
    #   - input_ids (List[List[int]])
    #   - attention_mask (List[List[int]])
    #   - bbox_phrase_exists (List[bool])
    #   - bbox_is_abnormal (List[bool])

    return tokenized_test_dataset


def get_tokenizer():
    checkpoint = "healx/gpt-2-pubmed-medium"
    tokenizer = GPT2Tokenizer.from_pretrained(checkpoint)
    tokenizer.pad_token = tokenizer.eos_token

    return tokenizer


def get_dataset():
    usecols = [
        "study_id",
        "mimic_image_file_path",
        "bbox_coordinates",
        "bbox_labels",
        "bbox_phrases",
        "bbox_phrase_exists",
        "bbox_is_abnormal",
    ]

    # all of the columns below are stored as strings in the csv_file
    # however, as they are actually lists, we apply the literal_eval func to convert them to lists
    converters = {
        "bbox_coordinates": literal_eval,
        "bbox_labels": literal_eval,
        "bbox_phrases": literal_eval,
        "bbox_phrase_exists": literal_eval,
        "bbox_is_abnormal": literal_eval,
    }

    datasets_as_dfs = {dataset: os.path.join(path_full_dataset, dataset) + ".csv" for dataset in ["test"]}

    datasets_as_dfs = {
        dataset: pd.read_csv(csv_file_path, usecols=usecols, converters=converters)
        for dataset, csv_file_path in datasets_as_dfs.items()
    }

    # bbox_phrases is a list of str
    # replace each bbox_phrase that is empty (i.e. "") by "#"
    # this is done such that model learns to generate the "#" symbol instead of "" for empty sentences
    # this is done because generated sentences that are "" (i.e. have len = 0) will cause problems when computing e.g. Bleu scores
    for dataset_df in datasets_as_dfs.values():
        dataset_df["bbox_phrases"] = dataset_df["bbox_phrases"].apply(
            lambda bbox_phrases: [phrase if len(phrase) != 0 else "#" for phrase in bbox_phrases]
        )

    datasets_as_dfs["test"] = datasets_as_dfs["test"][:NUM_IMAGES_TO_USE_IN_TEST_SET]

    raw_test_dataset = Dataset.from_pandas(datasets_as_dfs["test"])

    return raw_test_dataset


def main():
    # the datasets still contain the untokenized phrases
    raw_test_dataset = get_dataset()

    tokenizer = get_tokenizer()

    # tokenize the raw datasets
    tokenized_test_dataset = get_tokenized_dataset(tokenizer, raw_test_dataset)

    test_transforms = get_transforms()

    test_dataset_complete = CustomDataset("test", tokenized_test_dataset, test_transforms, log)

    test_loader = get_data_loader(tokenizer, test_dataset_complete)

    checkpoint = torch.load(
        f"/u/home/tanida/runs/full_model/run_{RUN}/checkpoints/{CHECKPOINT}.pt",
        map_location=torch.device("cpu"),
    )
    checkpoint["model"]["object_detector.rpn.head.conv.weight"] = checkpoint["model"].pop("object_detector.rpn.head.conv.0.0.weight")
    checkpoint["model"]["object_detector.rpn.head.conv.bias"] = checkpoint["model"].pop("object_detector.rpn.head.conv.0.0.bias")

    model = ReportGenerationModel()
    model.load_state_dict(checkpoint["model"])
    model.to(device, non_blocking=True)
    model.eval()

    del checkpoint

    evaluate_model(model, test_loader, tokenizer)


if __name__ == "__main__":
    main()
