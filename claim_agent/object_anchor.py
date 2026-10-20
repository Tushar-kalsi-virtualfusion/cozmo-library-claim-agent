"""Identity-only position evidence from reliable observed depth support.

An observed surface center is not the object's physical center, and its support
extent/radius are not item dimensions. Cropping and occlusion can move this
anchor. Association must additionally check session, class, competing tracks,
bounded distance and one-to-one assignment; never use this output for sizing.
"""
import numpy as np
from shapely import contains_xy
from shapely.geometry import MultiPoint, Polygon

from .models import FrameMetadata


def object_depth_anchor(meta: FrameMetadata, depth, confidence, polygon) -> dict | None:
    """Return robust world-position support for a detection, or abstain.

    Depth shares the unrotated RGB axes. Only ARKit high-confidence pixels
    inside an eroded detection are used. A separated background component is
    discarded only when a nearer component clearly dominates; balanced layers,
    background-dominated silhouettes and excessive depth spread are rejected.
    """
    if meta.tracking != "normal" or not meta.ar_session_id:
        return None
    try:
        quad = np.asarray(polygon, dtype=float)
        if (quad.shape != (4, 2) or not np.isfinite(quad).all()
                or np.any(quad < 0) or np.any(quad > 1)):
            return None
        poly = Polygon(quad)
        if not poly.is_valid or poly.area <= 0 or not poly.equals(poly.convex_hull):
            return None
        intrinsics = np.asarray(meta.intrinsics, dtype=float).reshape(3, 3, order="F")
        pose = np.asarray(meta.camera_transform, dtype=float).reshape(4, 4, order="F")
        if (not np.isfinite(intrinsics).all() or not np.isfinite(pose).all()
                or intrinsics[0, 0] <= 0 or intrinsics[1, 1] <= 0
                or not 0 <= intrinsics[0, 2] <= meta.image_width
                or not 0 <= intrinsics[1, 2] <= meta.image_height
                or not np.allclose([intrinsics[0, 1], intrinsics[1, 0]], [0, 0], atol=1e-5)
                or not np.allclose(intrinsics[2], [0, 0, 1], atol=1e-5)
                or not np.allclose(pose[3], [0, 0, 0, 1], atol=1e-5)
                or not np.allclose(pose[:3, :3].T @ pose[:3, :3], np.eye(3), atol=.01)
                or not np.isclose(np.linalg.det(pose[:3, :3]), 1., atol=.01)):
            return None
        d, c = np.asarray(depth, dtype=float), np.asarray(confidence)
        if (d.shape != (meta.depth_height, meta.depth_width) or c.shape != d.shape
                or not d.size or meta.image_width <= 0 or meta.image_height <= 0):
            return None
        height, width = d.shape
        pixel_quad = quad * [width, height]
        erosion = max(1., float(np.min(np.ptp(pixel_quad, axis=0))) * .06)
        interior = Polygon(pixel_quad).buffer(-erosion)
        if interior.is_empty or interior.area < 24:
            return None
        x0, y0, x1, y1 = interior.bounds
        x0, y0 = np.maximum(0, np.floor([x0, y0])).astype(int)
        x1, y1 = np.minimum([width, height], np.ceil([x1, y1])).astype(int)
        yy, xx = np.mgrid[y0:y1, x0:x1]
        mask = contains_xy(interior, xx + .5, yy + .5)
        local_depth = d[y0:y1, x0:x1]
        reliable = (mask & (c[y0:y1, x0:x1] == 2) & np.isfinite(local_depth)
                    & (local_depth > .15) & (local_depth < 5))
        if reliable.sum() < 24 or reliable.sum() / max(1, mask.sum()) < .03:
            return None
        z = local_depth[reliable]
        positions = np.column_stack((xx[reliable] + .5, yy[reliable] + .5))

        # Split separated depth layers without forcing a planar fit. Ignore tiny
        # isolated speckles, then require the nearest substantial layer to win.
        ordered = np.sort(z)
        gap = max(.12, float(np.median(z)) * .06)
        boundaries = np.flatnonzero(np.diff(ordered) > gap) + 1
        layers = np.split(ordered, boundaries)
        substantial = [layer for layer in layers if len(layer) >= max(8, len(z) * .08)]
        if not substantial:
            return None
        foreground = substantial[0]
        selected = (z >= foreground[0]) & (z <= foreground[-1])
        if selected.sum() < 24 or selected.mean() < .7:
            return None
        z, positions = z[selected], positions[selected]
        coverage = len(z) / max(1, mask.sum())
        if coverage < .03:
            return None
        # A narrow line or a few concentrated samples cannot locate the object
        # reliably, even if all their depth values agree perfectly.
        span = np.ptp(positions, axis=0)
        if (np.min(span) < 2 or np.max(span) < 3
                or MultiPoint(positions).convex_hull.area < max(12., interior.area * .015)):
            return None
        depth_span = float(np.quantile(z, .9) - np.quantile(z, .1))
        if depth_span > max(.25, min(.8, float(np.median(z)) * .45)):
            return None

        uv = positions * [meta.image_width / width, meta.image_height / height]
        camera = np.column_stack(((uv[:, 0] - intrinsics[0, 2]) / intrinsics[0, 0] * z,
                                  -(uv[:, 1] - intrinsics[1, 2]) / intrinsics[1, 1] * z, -z))
        world = (pose @ np.column_stack((camera, np.ones(len(camera)))).T).T[:, :3]
        center = np.median(world, axis=0)
        extent = np.quantile(world, .9, axis=0) - np.quantile(world, .1, axis=0)
        radius = float(np.quantile(np.linalg.norm(world - center, axis=1), .9))
        if not np.isfinite(center).all() or not .01 <= radius <= 2:
            return None
        return {"center": center.tolist(), "extent_m": extent.tolist(), "radius_m": radius,
                "samples": len(world), "coverage": float(coverage), "depth_span_m": depth_span,
                "frame_id": meta.id, "ar_session_id": meta.ar_session_id, "identity_only": True,
                "method": "Robust high-confidence interior depth support; identity only"}
    except (ValueError, TypeError, np.linalg.LinAlgError):
        return None
