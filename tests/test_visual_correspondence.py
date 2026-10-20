import io
import unittest
from collections import Counter
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image, ImageDraw

from claim_agent.visual_correspondence import FrameCorrespondence


def texture(seed=13, size=(640, 480)):
    rng = np.random.default_rng(seed)
    image = Image.new("L", size, 210)
    draw = ImageDraw.Draw(image)
    for _ in range(500):
        x, y = rng.integers([8, 8], np.array(size) - 20)
        radius = int(rng.integers(2, 10))
        shade = int(rng.integers(10, 245))
        draw.ellipse((x, y, x + radius, y + radius), fill=shade)
        draw.line((x, y, x + radius * 2, y - radius), fill=shade, width=2)
    return np.asarray(image)


def encoded(image):
    data = io.BytesIO()
    Image.fromarray(image).save(data, "PNG")
    return data.getvalue()


def transformed(image, polygon, angle=7, translation=(23, -9)):
    height, width = image.shape
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    matrix[:, 2] += translation
    result = cv2.warpAffine(image, matrix, (width, height), borderValue=210)
    points = np.asarray(polygon) * [width, height]
    target = np.column_stack((points, np.ones(len(points)))) @ matrix.T
    return result, (target / [width, height]).tolist()


class VisualCorrespondenceTests(unittest.TestCase):
    polygon = [[.25, .25], [.55, .25], [.55, .65], [.25, .65]]

    def test_rotation_and_translation_link_same_region_deterministically(self):
        source = texture()
        target, polygon = transformed(source, self.polygon)
        images = {"a": encoded(source), "b": encoded(target)}
        match = FrameCorrespondence(images.get)
        scores = [match.match_polygon("a", "b", self.polygon, polygon) for _ in range(3)]
        self.assertGreater(scores[0], .8)
        self.assertEqual(scores, [scores[0]] * 3)

    def test_unrelated_scene_and_blank_scene_do_not_match(self):
        images = {"a": encoded(texture()), "b": encoded(texture(77)),
                  "blank": encoded(np.full((480, 640), 150, dtype=np.uint8))}
        match = FrameCorrespondence(images.get)
        self.assertEqual(match.match_polygon("a", "b", self.polygon, self.polygon), 0)
        self.assertEqual(match.match_polygon("blank", "blank", self.polygon, self.polygon), 0)

    def test_identical_adjacent_objects_do_not_cross_match(self):
        source = texture().copy()
        tile = texture(42, (150, 160))
        source[160:320, 100:250] = tile
        source[160:320, 360:510] = tile
        left = (np.array([[100, 160], [250, 160], [250, 320], [100, 320]]) / [640, 480]).tolist()
        right = (np.array([[360, 160], [510, 160], [510, 320], [360, 320]]) / [640, 480]).tolist()
        target, _ = transformed(source, left)
        _, target_right = transformed(source, right)
        match = FrameCorrespondence({"a": encoded(source), "b": encoded(target)}.get)
        self.assertEqual(match.match_polygon("a", "b", left, target_right), 0)

    def test_repeated_pattern_without_unique_scene_support_is_rejected(self):
        tile = texture(19, (80, 80))
        repeated = np.tile(tile, (6, 8))
        match = FrameCorrespondence({"a": encoded(repeated), "b": encoded(repeated)}.get)
        left = [[.25, .33], [.375, .33], [.375, .5], [.25, .5]]
        right = [[.375, .33], [.5, .33], [.5, .5], [.375, .5]]
        self.assertEqual(match.match_polygon("a", "b", left, right), 0)

    def test_scene_match_without_object_features_is_rejected(self):
        source = texture().copy()
        source[120:313, 160:353] = 210
        target, polygon = transformed(source, self.polygon)
        match = FrameCorrespondence({"a": encoded(source), "b": encoded(target)}.get)
        self.assertEqual(match.match_polygon("a", "b", self.polygon, polygon), 0)

    def test_shared_object_without_surrounding_scene_support_is_rejected(self):
        source, target = texture(), texture(94).copy()
        target[120:313, 160:353] = source[120:313, 160:353]
        match = FrameCorrespondence({"a": encoded(source), "b": encoded(target)}.get)
        self.assertEqual(match.match_polygon("a", "b", self.polygon, self.polygon), 0)

    def test_collinear_scene_features_are_rejected(self):
        source = np.full((480, 640), 210, dtype=np.uint8)
        source[235:245] = texture()[235:245]
        target, polygon = transformed(source, self.polygon)
        match = FrameCorrespondence({"a": encoded(source), "b": encoded(target)}.get)
        self.assertEqual(match.match_polygon("a", "b", self.polygon, polygon), 0)

    def test_unavailable_library_invalid_images_and_polygons_fail_closed(self):
        match = FrameCorrespondence(lambda _: b"not an image")
        self.assertEqual(match.match_polygon("a", "b", self.polygon, self.polygon), 0)
        self.assertEqual(match.match_polygon("a", "b", [[0, 0]], self.polygon), 0)
        with patch("claim_agent.visual_correspondence.cv2", None):
            self.assertEqual(match.match_polygon("a", "b", self.polygon, self.polygon), 0)

    def test_feature_cache_reuses_frames_and_evicts_oldest(self):
        data = encoded(texture())
        reads = Counter()

        def read(frame):
            reads[frame] += 1
            return data

        match = FrameCorrespondence(read, max_frames=2)
        self.assertGreater(match.match_polygon("a", "b", self.polygon, self.polygon), .8)
        self.assertGreater(match.match_polygon("a", "b", self.polygon, self.polygon), .8)
        self.assertEqual(reads, {"a": 1, "b": 1})
        match.match_polygon("b", "c", self.polygon, self.polygon)
        match.match_polygon("c", "a", self.polygon, self.polygon)
        self.assertEqual(reads, {"a": 2, "b": 1, "c": 1})
