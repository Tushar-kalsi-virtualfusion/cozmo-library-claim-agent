"""Metric geometry only. No model or catalogue has access to these calculations."""
import numpy as np
import copy
from shapely.geometry import Polygon
from shapely.ops import unary_union

from .models import Dimensions, FrameMetadata, Room, FurnitureMeasurement


def unknown(reason):
    return {"height_cm": None, "thickness_cm": None, "world_corners": [], "reason": reason}


def measure_spine(meta: FrameMetadata, depth, confidence, polygon: list[list[float]], *, max_height_cm=100, max_thickness_cm=30) -> dict:
    """Fit a local depth plane, then intersect calibrated edge rays with it.

    JPEG pixels and depth share the unrotated ARKit camera axes. Intrinsics and
    pose arrive column-major. Depth is camera-forward (+Z); ARKit camera looks
    down -Z with +Y up, so the axis conversion is explicit below.
    """
    if meta.tracking != "normal":
        return unknown("AR tracking is not normal")
    try:
        pixels = np.asarray(polygon, dtype=float)
        if pixels.shape != (4, 2) or not np.isfinite(pixels).all() or (pixels < 0).any() or (pixels > 1).any():
            return unknown("Invalid normalized spine quadrilateral")
        poly = Polygon(pixels)
        if not poly.is_valid or poly.area <= 0 or not poly.equals(poly.convex_hull):
            return unknown("Spine polygon is degenerate or not convex")
        k = np.asarray(meta.intrinsics).reshape(3, 3, order="F")
        transform = np.asarray(meta.camera_transform).reshape(4, 4, order="F")
        if k[0, 0] <= 0 or k[1, 1] <= 0 or not np.allclose(k[2], [0, 0, 1], atol=1e-5):
            return unknown("Invalid camera intrinsics")
        if not np.allclose(transform[3], [0,0,0,1], atol=1e-5) or not np.allclose(transform[:3,:3].T @ transform[:3,:3], np.eye(3), atol=.01):
            return unknown("Invalid camera pose")
        d, c = np.asarray(depth, dtype=float), np.asarray(confidence)
        if d.shape != (meta.depth_height, meta.depth_width) or c.shape != d.shape or d.size == 0:
            return unknown("Aligned depth and confidence maps are missing")
        h, w = d.shape
        # A narrow spine often spans fewer than two LiDAR pixels. Fit its local
        # shelf plane from an expanded neighbourhood, retaining the median-depth
        # component. Do not interpret edge noise as the spine's thickness.
        lo, hi = pixels.min(axis=0), pixels.max(axis=0)
        x0, y0 = np.maximum([0, 0], np.floor(lo * [w, h] - 4)).astype(int)
        x1, y1 = np.minimum([w, h], np.ceil(hi * [w, h] + 4)).astype(int)
        yy, xx = np.mgrid[y0:y1, x0:x1]
        z = d[y0:y1, x0:x1]
        valid = (c[y0:y1, x0:x1] >= 2) & np.isfinite(z) & (z > .15) & (z < 5)
        if valid.sum() < 16:
            return unknown("Insufficient high-confidence depth samples")
        median = np.median(z[valid])
        valid &= np.abs(z - median) < .08
        if valid.sum() < 16:
            return unknown("Local depth has no stable plane")
        u = (xx[valid] + .5) * meta.image_width / w
        v = (yy[valid] + .5) * meta.image_height / h
        z = z[valid]
        points = np.column_stack(((u - k[0,2]) / k[0,0] * z, -(v-k[1,2]) / k[1,1] * z, -z))
        center = points.mean(axis=0)
        _, singular, vectors = np.linalg.svd(points-center, full_matrices=False)
        normal = vectors[-1]
        if singular[1] < .005:
            return unknown("Depth samples do not constrain a plane")
        residual = np.abs((points-center) @ normal)
        rms = float(np.sqrt(np.mean(residual**2)))
        if rms > .015:
            return unknown("Shelf plane depth residual exceeds 1.5 cm")
        px = pixels * [meta.image_width, meta.image_height]
        rays = np.column_stack(((px[:,0]-k[0,2])/k[0,0], -(px[:,1]-k[1,2])/k[1,1], -np.ones(4)))
        denominators = rays @ normal
        if np.min(np.abs(denominators)) < .15:
            return unknown("View angle is too oblique for reliable scale")
        distances = (center @ normal) / denominators
        if np.any(distances <= .15) or np.any(distances > 5):
            return unknown("Plane intersections are outside supported range")
        corners = rays * distances[:, None]
        edges = np.linalg.norm(np.roll(corners, -1, axis=0)-corners, axis=1)
        sides = [(edges[0]+edges[2])/2, (edges[1]+edges[3])/2]
        height, thickness = max(sides)*100, min(sides)*100
        if height < 3 or height > max_height_cm or thickness < .15 or thickness > max_thickness_cm:
            return unknown("Measured spine dimensions are implausible")
        world = (transform @ np.column_stack((corners, np.ones(4))).T).T[:, :3]
        return {"height_cm": round(float(height), 4), "thickness_cm": round(float(thickness), 4),
                "world_corners": world.tolist(), "reason": "", "method": "LiDAR local plane + calibrated corner rays",
                "plane_rms_m": rms, "samples": len(points), "depth_m": float(median),
                "frame_id": meta.id, "ar_session_id": meta.ar_session_id}
    except (ValueError, TypeError, np.linalg.LinAlgError):
        return unknown("Malformed or singular depth/calibration evidence")


def planar_polygon(corners):
    points = np.asarray(corners, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3 or len(points) < 3 or not np.isfinite(points).all():
        raise ValueError("Invalid surface corners")
    origin = points[0]
    u = points[1]-origin
    if np.linalg.norm(u) < 1e-6:
        raise ValueError("Degenerate surface")
    u /= np.linalg.norm(u)
    normal = np.cross(u, points[-1]-origin)
    if np.linalg.norm(normal) < 1e-6:
        raise ValueError("Degenerate surface")
    normal /= np.linalg.norm(normal)
    v = np.cross(normal, u)
    if np.max(np.abs((points-origin) @ normal)) > .05:
        raise ValueError("Nonplanar surface")
    p = Polygon(np.column_stack(((points-origin) @ u, (points-origin) @ v)))
    if not p.is_valid or p.area <= 1e-6:
        raise ValueError("Invalid surface polygon")
    return p, origin, u, v


def measure_room(geometry: dict) -> Room:
    room = Room(scale_method="RoomPlan/ARKit LiDAR metric world coordinates from the capture session",
                shape_method="Union of captured floor polygons; length/width are oriented bounding extents; gross wall surfaces")
    room.review_reasons.extend(str(issue) for issue in geometry.get("capture_issues",[])[:100])
    if geometry.get("capture_complete") is False:
        room.review_reasons.append("Scan in progress; room and furniture measurements are provisional")
    floors = []
    invalid_floor=False
    for surface in geometry.get("floors", []):
        try:
            points = np.asarray(surface["corners"], dtype=float)
            if not np.isfinite(points).all() or np.ptp(points[:,1]) > .08:
                raise ValueError("Floor is not horizontal")
            poly = Polygon(points[:, [0,2]])
            if not poly.is_valid or poly.area <= 0:
                raise ValueError("Invalid floor polygon")
            floors.append(poly)
        except (ValueError, KeyError, IndexError):
            invalid_floor=True
            room.review_reasons.append("An invalid floor polygon was rejected")
    if floors and not invalid_floor:
        floor = unary_union(floors)
        room.floor_area_m2 = float(floor.area)
        room.floor_area_ft2 = room.floor_area_m2 * 10.76391041671
        rectangle = np.asarray(floor.minimum_rotated_rectangle.exterior.coords)[:4]
        edges = np.linalg.norm(np.roll(rectangle, -1, axis=0)-rectangle, axis=1)
        room.length_m, room.width_m = float(max(edges)), float(min(edges))
        if floor.geom_type == "Polygon":
            room.floor_polygon_xz = np.asarray(floor.exterior.coords).tolist()
        else:
            room.review_reasons.append("Floor contains disconnected captured polygons; check coverage")
    else:
        room.review_reasons.append("Floor geometry missing or incomplete")
    areas, heights, shelving_areas, opening_areas = [], [], [], []
    bad_wall = False
    for wall in geometry.get("walls", []):
        try:
            p, origin, u, v = planar_polygon(wall["corners"])
            areas.append(p.area)
            heights.append(float(np.ptp(np.asarray(wall["corners"])[:,1])))
            for category, output in [("shelving", shelving_areas), ("openings", opening_areas)]:
                projections = []
                for obj in geometry.get(category, []):
                    if obj.get("wall_id") != wall["id"]:
                        continue
                    pts = np.asarray(obj["corners"], dtype=float)-origin
                    projection = Polygon(np.column_stack((pts @ u, pts @ v))).convex_hull
                    if projection.is_valid:
                        projections.append(projection.intersection(p))
                output.append(unary_union(projections).area if projections else 0.)
        except (ValueError, KeyError, IndexError):
            bad_wall = True
            room.review_reasons.append("Invalid wall or associated object geometry rejected")
    if areas and not bad_wall:
        room.wall_area_m2 = float(sum(areas))
        room.wall_area_ft2 = room.wall_area_m2 * 10.76391041671
        room.shelved_wall_area_m2 = float(sum(shelving_areas))
        room.openings_area_m2 = float(sum(opening_areas))
        room.net_wall_area_m2 = max(0., room.wall_area_m2-room.openings_area_m2)
        room.height_m = float(np.median(heights))
        if max(heights)-min(heights) > .1:
            room.review_reasons.append("Wall heights vary by more than 10 cm; reported height is the median")
    else:
        room.review_reasons.append("Wall geometry missing or incomplete")
    room.confidence = .8 if room.floor_area_m2 is not None and room.wall_area_m2 is not None and not room.review_reasons else .3
    if any("Curved surface" in issue for issue in room.review_reasons):
        room.wall_area_m2=room.wall_area_ft2=room.net_wall_area_m2=None
    return room


def valid_furniture(obj):
    try:
        center=np.asarray(obj["center"],dtype=float)
        dims=np.asarray(obj["dimensions"],dtype=float)
        return (bool(obj.get("id")) and center.shape==(3,) and dims.shape==(3,)
                and np.isfinite(center).all() and np.isfinite(dims).all()
                and (dims>.01).all() and (dims<20).all()
                and str(obj.get("confidence","medium")).casefold() in {"medium","high"})
    except (ValueError,TypeError,KeyError):
        return False


def reconcile_room(state) -> Room:
    """Link same-sweep visual categories to RoomPlan geometry after workers finish."""
    geometry=copy.deepcopy(state.geometry)
    geometry["objects"]=[obj for obj in geometry.get("objects",[]) if isinstance(obj,dict) and valid_furniture(obj)]
    geometry.setdefault("shelving",[])
    associations=[]
    unresolved_shelves=False
    used=set()
    category_map={"bookshelf":"storage","bookcase":"storage","shelving":"storage","shelf":"storage", "cabinet":"storage",
                  "chair":"chair","armchair":"chair","sofa":"sofa","couch":"sofa","table":"table","desk":"table",
                  "bed":"bed","television":"television","tv":"television"}
    for item in state.items:
        # Live RoomPlan revisions can remove or invalidate an earlier box.
        item.dimensions_cm=Dimensions()
        shelf=any(word in item.category.casefold() for word in ["shelf","shelv","bookcase"])
        target=next((native for word,native in category_map.items() if word in item.category.casefold()),None)
        candidates=[]
        anchor=getattr(state,"spatial_tracks",{}).get(item.id,{}).get("roomplan_id","")
        if anchor:
            candidates=[(0.,obj) for obj in geometry.get("objects",[]) if obj.get("id")==anchor and anchor not in used
                        and target and str(obj.get("category","")).casefold()==target]
        elif item.world_corners and target:
            center=np.asarray(item.world_corners).mean(axis=0)
            for obj in geometry.get("objects",[]):
                if obj.get("id") in used or target not in str(obj.get("category","")).casefold():
                    continue
                if len(obj.get("center",[]))!=3 or len(obj.get("dimensions",[]))!=3:
                    continue
                distance=float(np.linalg.norm(np.asarray(obj["center"])-center))
                if distance<.7:
                    candidates.append((distance,obj))
            candidates.sort(key=lambda pair:pair[0])
        if not candidates or (len(candidates)>1 and candidates[1][0]-candidates[0][0]<.12):
            if shelf: unresolved_shelves=True
            continue
        obj=candidates[0][1]
        used.add(obj["id"])
        w,h,d=obj["dimensions"]
        item.dimensions_cm=Dimensions(w=w*100,h=h*100,d=d*100)
        item.evidence_refs=list(dict.fromkeys(item.evidence_refs+[state.room.geometry_ref]))
        if shelf and obj.get("corners"):
            wall_matches=[]
            for wall in geometry.get("walls",[]):
                try:
                    _,origin,u,v=planar_polygon(wall["corners"])
                    normal=np.cross(u,v)
                    distance=abs(float((np.asarray(obj["center"])-origin)@normal))
                    if distance<=max(w,d)/2+.35:
                        wall_matches.append((distance,wall))
                except (ValueError,KeyError):
                    continue
            if wall_matches:
                wall=min(wall_matches,key=lambda pair:pair[0])[1]
                geometry["shelving"].append({"wall_id":wall["id"],"corners":obj["corners"]})
                associations.append({"item_id":item.id,"roomplan_object_id":obj["id"],"wall_id":wall["id"],"frame_ref":item.frame_ref})
            else:
                unresolved_shelves=True
    room=measure_room(geometry)
    room.geometry_ref=state.room.geometry_ref
    room.furniture=[FurnitureMeasurement(id=obj["id"],category=str(obj.get("category","Furniture")),
        dimensions_cm=Dimensions(w=obj["dimensions"][0]*100,h=obj["dimensions"][1]*100,d=obj["dimensions"][2]*100),
        confidence=str(obj.get("confidence","medium")),geometry_ref=room.geometry_ref) for obj in geometry["objects"]]
    room.shelving_associations=associations
    if unresolved_shelves:
        room.shelved_wall_area_m2=None
        room.review_reasons.append("Some visible shelving lacks an unambiguous wall/geometry association")
    return room


def occupied_shelf_run(observations: list[dict]) -> float | None:
    """Union horizontal book footprints per stable shelf level (stacks overlap)."""
    if not observations or any(not o.get("shelf") for o in observations):
        return None
    groups = {}
    for o in observations:
        if not np.isfinite([o["start"], o["end"]]).all():
            return None
        groups.setdefault(o["shelf"], []).append(sorted([o["start"], o["end"]]))
    total = 0.
    for spans in groups.values():
        current = None
        for start, end in sorted(spans):
            if current is None:
                current = [start, end]
            elif start <= current[1]:
                current[1] = max(current[1], end)
            else:
                total += current[1]-current[0]
                current = [start, end]
        total += current[1]-current[0]
    return total
