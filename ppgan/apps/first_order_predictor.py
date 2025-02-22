#  Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserve.
#
#Licensed under the Apache License, Version 2.0 (the "License");
#you may not use this file except in compliance with the License.
#You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#Unless required by applicable law or agreed to in writing, software
#distributed under the License is distributed on an "AS IS" BASIS,
#WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#See the License for the specific language governing permissions and
#limitations under the License.

import os
import sys
import cv2
import math

import yaml
import pickle
import imageio
import numpy as np
from tqdm import tqdm
from scipy.spatial import ConvexHull

import paddle
from ppgan.utils.download import get_path_from_url
from ppgan.utils.animate import normalize_kp
from ppgan.modules.keypoint_detector import KPDetector
from ppgan.models.generators.occlusion_aware import OcclusionAwareGenerator
from ppgan.faceutils import face_detection

from .base_predictor import BasePredictor


class FirstOrderPredictor(BasePredictor):
    def __init__(self,
                 output='output',
                 weight_path=None,
                 config=None,
                 relative=False,
                 adapt_scale=False,
                 find_best_frame=False,
                 best_frame=None,
                 ratio=1.0,
                 filename='result.mp4',
                 face_detector='sfd',
                 multi_person=False):
        if config is not None and isinstance(config, str):
            with open(config) as f:
                self.cfg = yaml.load(f, Loader=yaml.SafeLoader)
        elif isinstance(config, dict):
            self.cfg = config
        elif config is None:
            self.cfg = {
                'model': {
                    'common_params': {
                        'num_kp': 10,
                        'num_channels': 3,
                        'estimate_jacobian': True
                    },
                    'generator': {
                        'kp_detector_cfg': {
                            'temperature': 0.1,
                            'block_expansion': 32,
                            'max_features': 1024,
                            'scale_factor': 0.25,
                            'num_blocks': 5
                        },
                        'generator_cfg': {
                            'block_expansion': 64,
                            'max_features': 512,
                            'num_down_blocks': 2,
                            'num_bottleneck_blocks': 6,
                            'estimate_occlusion_map': True,
                            'dense_motion_params': {
                                'block_expansion': 64,
                                'max_features': 1024,
                                'num_blocks': 5,
                                'scale_factor': 0.25
                            }
                        }
                    }
                }
            }
            if weight_path is None:
                vox_cpk_weight_url = 'https://paddlegan.bj.bcebos.com/applications/first_order_model/vox-cpk.pdparams'
                weight_path = get_path_from_url(vox_cpk_weight_url)

        self.weight_path = weight_path
        if not os.path.exists(output):
            os.makedirs(output)
        self.output = output
        self.filename = filename
        self.relative = relative
        self.adapt_scale = adapt_scale
        self.find_best_frame = find_best_frame
        self.best_frame = best_frame
        self.ratio = ratio
        self.face_detector = face_detector
        self.generator, self.kp_detector = self.load_checkpoints(
            self.cfg, self.weight_path)
        self.multi_person = multi_person

    def run(self, source_image, driving_video):
        def get_prediction(face_image):
            if self.find_best_frame or self.best_frame is not None:
                i = self.best_frame if self.best_frame is not None else self.find_best_frame_func(
                    source_image, driving_video)

                print("Best frame: " + str(i))
                driving_forward = driving_video[i:]
                driving_backward = driving_video[:(i + 1)][::-1]
                predictions_forward = self.make_animation(
                    face_image,
                    driving_forward,
                    self.generator,
                    self.kp_detector,
                    relative=self.relative,
                    adapt_movement_scale=self.adapt_scale)
                predictions_backward = self.make_animation(
                    face_image,
                    driving_backward,
                    self.generator,
                    self.kp_detector,
                    relative=self.relative,
                    adapt_movement_scale=self.adapt_scale)
                predictions = predictions_backward[::-1] + predictions_forward[
                    1:]
            else:
                predictions = self.make_animation(
                    face_image,
                    driving_video,
                    self.generator,
                    self.kp_detector,
                    relative=self.relative,
                    adapt_movement_scale=self.adapt_scale)
            return predictions

        source_image = imageio.imread(source_image)
        reader = imageio.get_reader(driving_video)
        fps = reader.get_meta_data()['fps']
        driving_video = []
        try:
            for im in reader:
                driving_video.append(im)
        except RuntimeError:
            pass
        reader.close()

        driving_video = [
            cv2.resize(frame, (256, 256)) / 255.0 for frame in driving_video
        ]
        results = []

        # for single person
        if not self.multi_person:
            h, w, _ = source_image.shape
            source_image = cv2.resize(source_image, (256, 256)) / 255.0
            predictions = get_prediction(source_image)
            imageio.mimsave(os.path.join(self.output, self.filename), [
                cv2.resize((frame * 255.0).astype('uint8'), (h, w))
                for frame in predictions
            ])
            return

        bboxes = self.extract_bbox(source_image.copy())
        print(str(len(bboxes)) + " persons have been detected")
        if len(bboxes) <= 1:
            h, w, _ = source_image.shape
            source_image = cv2.resize(source_image, (256, 256)) / 255.0
            predictions = get_prediction(source_image)
            imageio.mimsave(os.path.join(self.output, self.filename), [
                cv2.resize((frame * 255.0).astype('uint8'), (h, w))
                for frame in predictions
            ])
            return

        # for multi person
        for rec in bboxes:
            face_image = source_image.copy()[rec[1]:rec[3], rec[0]:rec[2]]
            face_image = cv2.resize(face_image, (256, 256)) / 255.0
            predictions = get_prediction(face_image)
            results.append({'rec': rec, 'predict': predictions})

        out_frame = []

        for i in range(len(driving_video)):
            frame = source_image.copy()
            for result in results:
                x1, y1, x2, y2, _ = result['rec']
                h = y2 - y1
                w = x2 - x1
                out = result['predict'][i] * 255.0
                out = cv2.resize(out.astype(np.uint8), (x2 - x1, y2 - y1))
                if len(results) == 1:
                    frame[y1:y2, x1:x2] = out
                else:
                    patch = np.zeros(frame.shape).astype('uint8')
                    patch[y1:y2, x1:x2] = out
                    mask = np.zeros(frame.shape[:2]).astype('uint8')
                    cx = int((x1 + x2) / 2)
                    cy = int((y1 + y2) / 2)
                    cv2.circle(mask, (cx, cy), math.ceil(h * self.ratio),
                               (255, 255, 255), -1, 8, 0)
                    frame = cv2.copyTo(patch, mask, frame)

            out_frame.append(frame)
        imageio.mimsave(os.path.join(self.output, self.filename),
                        [frame for frame in out_frame],
                        fps=fps)

    def load_checkpoints(self, config, checkpoint_path):

        generator = OcclusionAwareGenerator(
            **config['model']['generator']['generator_cfg'],
            **config['model']['common_params'])

        kp_detector = KPDetector(
            **config['model']['generator']['kp_detector_cfg'],
            **config['model']['common_params'])

        checkpoint = paddle.load(self.weight_path)
        generator.set_state_dict(checkpoint['generator'])

        kp_detector.set_state_dict(checkpoint['kp_detector'])

        generator.eval()
        kp_detector.eval()

        return generator, kp_detector

    def make_animation(self,
                       source_image,
                       driving_video,
                       generator,
                       kp_detector,
                       relative=True,
                       adapt_movement_scale=True):
        with paddle.no_grad():
            predictions = []
            source = paddle.to_tensor(source_image[np.newaxis].astype(
                np.float32)).transpose([0, 3, 1, 2])

            driving = paddle.to_tensor(
                np.array(driving_video)[np.newaxis].astype(
                    np.float32)).transpose([0, 4, 1, 2, 3])
            kp_source = kp_detector(source)
            kp_driving_initial = kp_detector(driving[:, :, 0])

            for frame_idx in tqdm(range(driving.shape[2])):
                driving_frame = driving[:, :, frame_idx]
                kp_driving = kp_detector(driving_frame)
                kp_norm = normalize_kp(
                    kp_source=kp_source,
                    kp_driving=kp_driving,
                    kp_driving_initial=kp_driving_initial,
                    use_relative_movement=relative,
                    use_relative_jacobian=relative,
                    adapt_movement_scale=adapt_movement_scale)
                out = generator(source, kp_source=kp_source, kp_driving=kp_norm)

                predictions.append(
                    np.transpose(out['prediction'].numpy(), [0, 2, 3, 1])[0])
        return predictions

    def find_best_frame_func(self, source, driving):
        import face_alignment

        def normalize_kp(kp):
            kp = kp - kp.mean(axis=0, keepdims=True)
            area = ConvexHull(kp[:, :2]).volume
            area = np.sqrt(area)
            kp[:, :2] = kp[:, :2] / area
            return kp

        fa = face_alignment.FaceAlignment(face_alignment.LandmarksType._2D,
                                          flip_input=True)

        kp_source = fa.get_landmarks(255 * source)[0]
        kp_source = normalize_kp(kp_source)
        norm = float('inf')
        frame_num = 0
        for i, image in tqdm(enumerate(driving)):
            kp_driving = fa.get_landmarks(255 * image)[0]
            kp_driving = normalize_kp(kp_driving)
            new_norm = (np.abs(kp_source - kp_driving)**2).sum()
            if new_norm < norm:
                norm = new_norm
                frame_num = i
        return frame_num

    def extract_bbox(self, image):
        detector = face_detection.FaceAlignment(
            face_detection.LandmarksType._2D,
            flip_input=False,
            face_detector=self.face_detector)

        frame = [image]
        predictions = detector.get_detections_for_image(np.array(frame))
        person_num = len(predictions)
        if person_num == 0:
            return np.array([])
        results = []
        face_boxs = []
        h, w, _ = image.shape
        for rect in predictions:
            bh = rect[3] - rect[1]
            bw = rect[2] - rect[0]
            cy = rect[1] + int(bh / 2)
            cx = rect[0] + int(bw / 2)
            margin = max(bh, bw)
            y1 = max(0, cy - margin)
            x1 = max(0, cx - int(0.8 * margin))
            y2 = min(h, cy + margin)
            x2 = min(w, cx + int(0.8 * margin))
            area = (y2 - y1) * (x2 - x1)
            results.append([x1, y1, x2, y2, area])
        # if a person has more than one bbox, keep the largest one
        # maybe greedy will be better?
        sorted(results, key=lambda area: area[4], reverse=True)
        results_box = [results[0]]
        for i in range(1, person_num):
            num = len(results_box)
            add_person = True
            for j in range(num):
                pre_person = results_box[j]
                iou = self.IOU(pre_person[0], pre_person[1], pre_person[2],
                               pre_person[3], pre_person[4], results[i][0],
                               results[i][1], results[i][2], results[i][3],
                               results[i][4])
                if iou > 0.5:
                    add_person = False
                    break
            if add_person:
                results_box.append(results[i])
        boxes = np.array(results_box)
        return boxes

    def IOU(self, ax1, ay1, ax2, ay2, sa, bx1, by1, bx2, by2, sb):
        #sa = abs((ax2 - ax1) * (ay2 - ay1))
        #sb = abs((bx2 - bx1) * (by2 - by1))
        x1, y1 = max(ax1, bx1), max(ay1, by1)
        x2, y2 = min(ax2, bx2), min(ay2, by2)
        w = x2 - x1
        h = y2 - y1
        if w < 0 or h < 0:
            return 0.0
        else:
            return 1.0 * w * h / (sa + sb - w * h)
