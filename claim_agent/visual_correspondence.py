"""Conservative, local image correspondence; never establishes identity alone.

Callers must constrain candidates by AR session, camera motion and object class.
A score is the overlap of a verified scene transform with the target polygon,
not an appearance similarity score or evidence of metric size. No images leave
the server, and missing OpenCV or insufficient evidence simply produces zero.
"""
from collections import OrderedDict
from dataclasses import dataclass
import io
from threading import RLock
from typing import Callable

import numpy as np
from PIL import Image
from shapely.geometry import MultiPoint, Point, Polygon, box

try:
    import cv2
except (ImportError, OSError):  # A missing native runtime must not break capture.
    cv2 = None


@dataclass(frozen=True)
class _Features:
    size: np.ndarray
    points: np.ndarray
    descriptors: np.ndarray


@dataclass(frozen=True)
class _Correspondence:
    matrix: np.ndarray
    source: np.ndarray
    target: np.ndarray
    inliers: np.ndarray


def _polygon(points, size):
    values = np.asarray(points, dtype=float)
    if (values.ndim != 2 or values.shape[1] != 2 or not 3 <= len(values) <= 16
            or not np.isfinite(values).all() or np.any(values < 0) or np.any(values > 1)):
        return None
    polygon = Polygon(values * size)
    return polygon if polygon.is_valid and polygon.area >= 48 else None


def _inside(points, polygon):
    return np.array([polygon.covers(Point(point)) for point in points], dtype=bool)


def _distributed(points, area, minimum_area):
    """Count distinct spatial support, not SIFT's multiple orientations."""
    if len(points) < 4 or MultiPoint(points).convex_hull.area < area * minimum_area:
        return False
    eigenvalues = np.linalg.eigvalsh(np.cov(points.T))
    return bool(eigenvalues[1] > 1 and eigenvalues[0] / eigenvalues[1] >= .01)


class FrameCorrespondence:
    """Bounded SIFT/scene correspondence using normalized image polygons.

    ``read_image(frame_id)`` returns encoded image bytes or None. Cache keys
    assume immutable frame IDs within one sweep; create one instance per sweep.
    Every nonzero result passed geometric and local-support checks; callers
    should still require >= .55 overlap and reject competing track candidates.
    """

    def __init__(self, read_image: Callable[[str], bytes | None], *, max_frames=12):
        self._read_image = read_image
        self._max_frames = max(2, min(12, int(max_frames)))
        self._frames = OrderedDict()
        self._pairs = OrderedDict()
        self._lock = RLock()

    def match_polygon(self, source_frame_id, target_frame_id, source_polygon, target_polygon) -> float:
        if cv2 is None or not hasattr(cv2, "SIFT_create"):
            return 0.
        with self._lock:
            try:
                source = self._features(source_frame_id)
                target = self._features(target_frame_id)
                if source is None or target is None:
                    return 0.
                a = _polygon(source_polygon, source.size)
                b = _polygon(target_polygon, target.size)
                if a is None or b is None:
                    return 0.
                pair = self._correspondence(source_frame_id, target_frame_id, source, target)
                if pair is None:
                    return 0.
                return self._score(pair, a, b, source.size, target.size)
            except (OSError, ValueError, TypeError, cv2.error, np.linalg.LinAlgError):
                return 0.

    def _features(self, frame_id):
        if frame_id in self._frames:
            self._frames.move_to_end(frame_id)
            return self._frames[frame_id]
        features = None
        data = self._read_image(frame_id)
        if data:
            with Image.open(io.BytesIO(data)) as image:
                image = image.convert("L")
                image.thumbnail((960, 960), Image.Resampling.LANCZOS)
                pixels = np.asarray(image)
            detector = cv2.SIFT_create(nfeatures=2400, contrastThreshold=.025)
            keypoints, descriptors = detector.detectAndCompute(pixels, None)
            if descriptors is not None and len(keypoints) >= 16:
                features = _Features(np.array(image.size, dtype=float),
                                     np.float32([point.pt for point in keypoints]), descriptors)
        self._frames[frame_id] = features
        while len(self._frames) > self._max_frames:
            expired, _ = self._frames.popitem(last=False)
            for key in list(self._pairs):
                if expired in key:
                    del self._pairs[key]
        return features

    def _correspondence(self, source_id, target_id, source, target):
        key = (source_id, target_id)
        if key in self._pairs:
            self._pairs.move_to_end(key)
            return self._pairs[key]
        result = self._fit(source, target)
        self._pairs[key] = result
        while len(self._pairs) > 36:
            self._pairs.popitem(last=False)
        return result

    @staticmethod
    def _fit(source, target):
        matcher = cv2.BFMatcher(cv2.NORM_L2)

        def unique_matches(a, b):
            return {first.queryIdx: first for pair in matcher.knnMatch(a, b, k=2)
                    if len(pair) == 2 for first, second in [pair]
                    if first.distance < .72 * second.distance}

        forward = unique_matches(source.descriptors, target.descriptors)
        reverse = unique_matches(target.descriptors, source.descriptors)
        matches = sorted((match for match in forward.values()
                          if match.trainIdx in reverse and reverse[match.trainIdx].trainIdx == match.queryIdx),
                         key=lambda match: (match.distance, match.queryIdx, match.trainIdx))
        # The same corner often has several orientations. These must not inflate
        # the count or make a cluster masquerade as a distributed consensus.
        selected, seen_a, seen_b = [], set(), set()
        for match in matches:
            a = tuple(np.rint(source.points[match.queryIdx] / 3).astype(int))
            b = tuple(np.rint(target.points[match.trainIdx] / 3).astype(int))
            if a not in seen_a and b not in seen_b:
                selected.append(match)
                seen_a.add(a)
                seen_b.add(b)
        if len(selected) < 16:
            return None
        a = np.float32([source.points[match.queryIdx] for match in selected])
        b = np.float32([target.points[match.trainIdx] for match in selected])
        matrix, mask = cv2.findHomography(a, b, cv2.RANSAC, 3., maxIters=2000, confidence=.995)
        if matrix is None or mask is None or not np.isfinite(matrix).all():
            return None
        inliers = mask.ravel().astype(bool)
        if inliers.sum() < 16 or inliers.mean() < .45:
            return None
        if (not _distributed(a[inliers], np.prod(source.size), .015)
                or not _distributed(b[inliers], np.prod(target.size), .015)):
            return None
        # Require the fitted relation to work in both directions; a collapsed
        # transform can otherwise project a small cluster onto a broad region.
        inverse = np.linalg.inv(matrix)
        backward = cv2.perspectiveTransform(b[inliers, None], inverse)[:, 0]
        if np.median(np.linalg.norm(backward - a[inliers], axis=1)) > 3:
            return None
        return _Correspondence(matrix, a, b, inliers)

    @staticmethod
    def _score(pair, source_polygon, target_polygon, source_size, target_size):
        source_points = np.asarray(source_polygon.exterior.coords[:-1], dtype=np.float32)
        homogeneous = np.column_stack((source_points, np.ones(len(source_points)))) @ pair.matrix.T
        denominators = homogeneous[:, 2]
        if np.min(np.abs(denominators)) < 1e-6 or np.any(denominators * denominators[0] <= 0):
            return 0.
        projected = Polygon(homogeneous[:, :2] / denominators[:, None])
        if not projected.is_valid or projected.area <= 0:
            return 0.
        projected = projected.intersection(box(0, 0, *target_size))
        overlap = projected.intersection(target_polygon).area / projected.union(target_polygon).area
        if overlap < .55:
            return 0.
        # Features must be inside both proposed object regions, away from their
        # boundaries; surrounding scene alone cannot identify a blank object.
        inside_a = _inside(pair.source, source_polygon.buffer(-3))
        inside_b = _inside(pair.target, target_polygon.buffer(-3))
        local = pair.inliers & inside_a & inside_b
        if local.sum() < 4:
            return 0.
        if (not _distributed(pair.source[local], source_polygon.area, .025)
                or not _distributed(pair.target[local], target_polygon.area, .025)):
            return 0.
        if local.sum() / max(inside_a.sum(), inside_b.sum(), 1) < .6:
            return 0.
        # Copyable text/texture on an object is insufficient. A shared transform
        # must also have independent scene support outside that object.
        external = (pair.inliers & ~_inside(pair.source, source_polygon.buffer(8))
                    & ~_inside(pair.target, target_polygon.buffer(8)))
        if external.sum() < 6:
            return 0.
        return float(min(1., overlap))
