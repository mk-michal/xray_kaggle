import copy
import datetime
import logging
import os
import shelve
from typing import Dict, List, Tuple

import albumentations as A
import numpy as np
import pandas as pd
import pydicom
import torch
import torchvision


class Averager:
    def __init__(self):
        self.loss_box_reg = 0.0
        self.loss_objectness = 0.0
        self.loss_rpn_box_reg = 0.0
        self.current_total = 0.0
        self.iterations = 0.0
        self.loss_classifier = 0.0

    def send(self, value):
        self.current_total += value
        self.iterations += 1

    @property
    def value(self):
        if self.iterations == 0:
            return 0
        else:
            return 1.0 * self.current_total / self.iterations

    @property
    def value_all_losses(self):
        if self.iterations == 0:
            return 0
        else:
            return {
                'loss_classifier': self.loss_classifier/self.iterations,
                'loss_box_reg': self.loss_box_reg/self.iterations,
                'loss_objectness': self.loss_objectness/self.iterations,
                'loss_rpn_box_reg': self.loss_rpn_box_reg/self.iterations
            }

    def reset(self):
        self.current_total = 0.0
        self.iterations = 0.0

    def reset_all_losses(self):
        self.loss_classifier = 0.0
        self.loss_box_reg = 0.0
        self.loss_objectness = 0.0
        self.loss_rpn_box_reg = 0.0
        self.iterations = 0.0


    def send_all(self, losses: Tuple[float, float, float, float]):
        self.loss_classifier += losses[0]
        self.loss_box_reg += losses[1]
        self.loss_objectness += losses[2]
        self.loss_rpn_box_reg += losses[3]



def my_custom_collate(x):
    x = [(a,b) for a,b in x]
    return list(zip(*x))


def create_eval_df(results: List[Dict[str, np.array]], description: List[Dict[str, np.array]]):

    image_ids = []
    string_scores = []
    for result, desc in zip(results, description):
        for bbox, score, pred_class in zip(result['boxes'], result['scores'], result['labels']):
            if int(pred_class) == 0:
                string_scores.append(f'0 1.0 0 0 1 1')
                image_ids.append(desc['file_name'])
            else:
                bboxes_str = ' '.join(list(map(str, bbox.astype(int))))

                string_scores.append(f'{int(pred_class)} {score} {bboxes_str}')
                image_ids.append(desc['file_name'])

    eval_df = pd.DataFrame({'image_id': image_ids, 'PredictionString': string_scores})

    return eval_df


def create_true_df(descriptions: List[Dict[str, np.array]]):
    true_df = pd.DataFrame(
        columns=['image_id', 'class_name', 'class_id', 'x_min', 'y_min', 'x_max', 'y_max']
    )

    for desc in descriptions:
        bbox_df_true = pd.DataFrame(
            desc['boxes'], columns=['x_min', 'y_min', 'x_max', 'y_max']
        )

        bbox_df_true['class_id'] = desc['labels']
        bbox_df_true['image_id'] = desc['file_name']

        true_df = true_df.append(bbox_df_true)

    return true_df


def create_submission_df(
    results: List[Dict[str, torch.Tensor ]], image_ids: List[str]
):
    all_rows = []
    for image_id, result in zip(image_ids, results):
        labels = [l-1 if int(l) != 0 else 14 for l in result['labels']]
        if len(result['boxes']) > 0:
            all_rows.append({
                'PredictionString': format_prediction_string(labels, result['boxes'], result['scores']),
                'image_id': image_id
            })
        else:
            all_rows.append({'PredictionString': '14 1.0 0 0 1 1', 'image_id': image_id})
    return pd.DataFrame(all_rows)


def time_str(fmt=None):
    if fmt is None:
        fmt = '%Y-%m-%d_%H:%M:%S'

    #     time.strftime(format[, t])
    return datetime.datetime.today().strftime(fmt)

def get_augmentation(prob = 0.8):
    return A.Compose(
        [A.augmentations.RandomBrightnessContrast(p=prob),
         A.augmentations.Equalize(p=prob),
         A.augmentations.RGBShift(p=prob),
         A.augmentations.Flip(p=prob)
         ]
        ,
        bbox_params=A.BboxParams(format='pascal_voc'),
        p=1
    )

def filter_radiologist_findings_deprecated(
    boxes: torch.Tensor,
    labels: torch.Tensor,
    iou_threshold: float = 0.4
) -> Tuple[torch.Tensor, torch.Tensor]:
    boxes_index_set = set(list(range(len(boxes))))
    final_boxes = []
    final_labels = []
    while boxes_index_set:
        index = boxes_index_set.pop()
        label = labels[index].item()
        box = boxes[index]
        labels_subset = labels[list(boxes_index_set)] == label
        boxes_sample = boxes[list(boxes_index_set)][labels_subset]
        boxes_index = torch.Tensor([i for i, l in enumerate(labels) if l == label and i in boxes_index_set])

        boxes_iou = torchvision.ops.box_iou(box.unsqueeze(0), boxes_sample)
        boxes_iou_bool = (boxes_iou > iou_threshold).squeeze(0)
        if boxes_iou_bool.sum().item() >= 1:
            boxes_sample_iou_high = boxes_sample[boxes_iou_bool]
            index_iou_high = boxes_index[boxes_iou_bool]
            final_box = torch.mean(boxes_sample_iou_high, axis = 0)
            final_boxes.append(final_box)
            final_labels.append(label)
            for i in index_iou_high:
                if i.item() in boxes_index_set:
                    boxes_index_set.remove(i.item())
    if len(final_boxes) > 0:
        return torch.stack(final_boxes), torch.tensor(final_labels, dtype=torch.long)
    else:
        return torch.Tensor([[]]), torch.Tensor([])


def filter_radiologist_findings(
    boxes: torch.Tensor,
    labels: torch.Tensor,
    iou_threshold: float = 0.4,
) -> Tuple[torch.Tensor, torch.Tensor]:
    # rescale by height and width

    boxes_final, labels_final = [], []

    for label in torch.unique(labels):
        boxes_subset = boxes[labels == label.item()]
        if len(boxes_subset) == 1:
            boxes_final.append(boxes_subset[0])
            labels_final.append(label.item())
        else:
            index_boxes = torchvision.ops.nms(
                boxes=boxes_subset,
                scores=torch.ones(len(boxes_subset)),
                iou_threshold=iou_threshold
            )
            for i in index_boxes:
                boxes_final.append(boxes_subset[i])
                labels_final.append(label.item())
    labels_tensor = torch.Tensor(labels_final)
    boxes_tensor = torch.stack(boxes_final)

    return boxes_tensor, labels_tensor


def transform_no_findings(results):
    new_dict = copy.deepcopy(results)
    for index in range(len(results)):
        for box_n, (box, label) in enumerate(
            zip(results[index]['boxes'], results[index]['labels'])):
            if label == 14:
                new_dict[index]['boxes'][box_n] = torch.tensor([0, 0, 1, 1])
    return new_dict


def no_findings_to_ones(results: List[Dict[str, np.array]]):
    new_dict = copy.deepcopy(results)
    for index in range(len(results)):
        if results[index]['boxes'].size == 0:
            new_dict[index]['boxes'] = np.array([[0, 0, 1, 1]])
            new_dict[index]['labels'] = np.array([0])
            new_dict[index]['scores'] = np.array([1.0])

    return new_dict


def do_nms(string_row):
    example = string_row.split(' ')
    example_edited = []
    scores = []
    classes = []
    for index in range(int(len(example) / 6)):
        scores.append(float(example[index * 6 + 1]))
        classes.append(int(float(example[index * 6])))

        bbox = list(map(float, example[(index * 6 + 2):(index * 6 + 6)]))
        example_edited.append(bbox)
    final_example = torch.tensor(example_edited, dtype=torch.float32, device='cpu')
    index_after_nms = torchvision.ops.nms(final_example, torch.tensor(scores).float(), 0.4)

    scores_updated = torch.tensor(scores)[index_after_nms]
    classes_updated = torch.tensor(classes)[index_after_nms]
    final_example_updated = final_example[index_after_nms]
    list_of_lists = torch.cat(
        [classes_updated.unsqueeze(dim=1), scores_updated.unsqueeze(dim=1), final_example_updated],
        axis=1).tolist()
    for l in list_of_lists:
        l[0] = int(l[0])

    final_output = ' '.join([str(element) for puf in list_of_lists for element in puf])
    return final_output

def resize_bbox(
    bbox_coord:Tuple[float, float, float, float],
    curr_size: Tuple[int, int],
    new_size: Tuple[int, int]
):
    x0_new = float(bbox_coord[0]) * new_size[0] / curr_size[0]
    x1_new = float(bbox_coord[2]) * new_size[0] / curr_size[0]
    y0_new = float(bbox_coord[1]) * new_size[1] / curr_size[1]
    y1_new = float(bbox_coord[3]) * new_size[1] / curr_size[1]
    return [x0_new, y0_new, x1_new, y1_new]

def rescale_to_original_size(
    output_file,
    test_data_desc: pd.DataFrame,
    current_size: Tuple[int, int] = (1024, 1024)
):

    new_predicted_strings = []
    for i, (string, image_id) in enumerate(zip(output_file.PredictionString, output_file.image_id)):
        string_list = string.split(' ')
        orig_shape = (test_data_desc.loc[test_data_desc.image_id == image_id].iloc[0].height,
                      test_data_desc.loc[test_data_desc.image_id == image_id].iloc[0].width)

        new_string = []
        for index in range(int(len(string_list) / 6)):
            current_string = string_list[index * 6: index * 6 + 6]
            if int(current_string[0]) != 14:
                new_bbox_coord = resize_bbox(
                    bbox_coord=current_string[2:],
                    curr_size=current_size,
                    new_size=orig_shape
                )

                new_bbox_coord = [str(current_string[0]), str(current_string[1])] + list(map(str, new_bbox_coord))
                new_string.extend(new_bbox_coord)
            else:
                new_string.extend(['14','1', '0','0','1','1'])
        new_predicted_strings.append(' '.join(new_string))
        if (i+1) % 10 == 0:
            print(f'Episode {i}', flush = True)

    return pd.DataFrame({'PredictionString': new_predicted_strings, 'image_id': output_file.image_id})

def format_prediction_string(labels, boxes, scores):
    pred_strings = []
    for j in zip(labels, scores, boxes):
        pred_strings.append("{0} {1:.4f} {2} {3} {4} {5}".format(
            j[0], j[1], j[2][0], j[2][1], j[2][2], j[2][3]))

    return " ".join(pred_strings)


def define_logger(name: str, folder: str = None, filehandler: bool = True, streamhandler: bool = True):
    logger = logging.getLogger(name)
    logFormatter = logging.Formatter(
        "%(asctime)s %(name)s [%(levelname)-5.5s]  %(message)s"
    )

    if filehandler:
        if folder is None:
            raise ValueError('Folder needs to be set if filehandler is enabled')
        fileHandler = logging.FileHandler(os.path.join(folder, 'model_log.log'))
        fileHandler.setFormatter(logFormatter)
        logger.addHandler(fileHandler)

    if streamhandler:
        streamhandler = logging.StreamHandler()
        streamhandler.setFormatter(logFormatter)
        logger.addHandler(streamhandler)

    return logger
