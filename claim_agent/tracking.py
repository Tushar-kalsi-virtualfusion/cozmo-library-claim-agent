"""Physical association with bounded view history; text alone never merges objects."""
from uuid import uuid4
import re
import unicodedata

import numpy as np
from shapely.geometry import Polygon, MultiPoint, box


def category_matches(first, second):
    def words(value):
        return set(re.findall(r"\w+",unicodedata.normalize("NFKC",value or "").casefold()))
    a,b=words(first),words(second)
    return bool(a and b and (a <= b or b <= a))


def view_metadata(value):
    return {key:value[key] for key in ("ar_session_id","tracking","image_width","image_height","intrinsics","camera_transform") if key in value}


def project_points(points, metadata):
    try:
        if metadata.get("tracking") != "normal": return None
        p=np.asarray(points,dtype=float)
        if p.ndim!=2 or p.shape[1]!=3 or len(p)<4 or not np.isfinite(p).all(): return None
        transform=np.asarray(metadata["camera_transform"]).reshape(4,4,order="F")
        k=np.asarray(metadata["intrinsics"]).reshape(3,3,order="F")
        camera=(np.linalg.inv(transform) @ np.column_stack((p,np.ones(len(p)))).T).T
        depth=-camera[:,2]
        if np.any(depth<=.1): return None
        xy=np.column_stack((k[0,0]*camera[:,0]/depth+k[0,2],-k[1,1]*camera[:,1]/depth+k[1,2]))
        xy/=np.array([metadata["image_width"],metadata["image_height"]])
        projected=MultiPoint(xy).convex_hull.intersection(box(0,0,1,1))
        return projected if projected.area>1e-6 else None
    except (ValueError,KeyError,TypeError,np.linalg.LinAlgError):
        return None


def roomplan_anchor(observation, geometry):
    meta=observation.get("metadata",{})
    if not meta.get("ar_session_id") or meta.get("ar_session_id")!=geometry.get("ar_session_id"): return ""
    polygon=observation.get("polygon",[])
    if len(polygon)!=4: return ""
    target=Polygon(polygon)
    if not target.is_valid or target.area<=0: return ""
    candidates=[]
    for obj in geometry.get("objects",[]):
        if (not obj.get("id") or obj.get("confidence") not in {"medium","high"}
                or not category_matches(observation.get("category",""),obj.get("category",""))): continue
        support=observation.get("depth_anchor")
        if support:
            corners=np.asarray(obj.get("corners",[]),dtype=float)
            if corners.shape!=(8,3) or not np.isfinite(corners).all(): continue
            # Native RoomGeometry emits x/y/z nested corner order. Recover the
            # actual box axes; a world AABB includes empty corners of rotated boxes.
            edges=corners[[4,2,1]]-corners[0]
            lengths=np.linalg.norm(edges,axis=1)
            if np.any(lengths<=.01): continue
            axes=edges/lengths[:,None]
            if not np.allclose(axes@axes.T,np.eye(3),atol=.01): continue
            expected=np.array([corners[0]+x*edges[0]+y*edges[1]+z*edges[2]
                               for x in [0,1] for y in [0,1] for z in [0,1]])
            if not np.allclose(corners,expected,atol=.01): continue
            local=axes@(np.asarray(support["center"])-corners[0])
            outside=np.maximum(np.maximum(-local,local-lengths),0)
            if np.linalg.norm(outside)>.12: continue
        projected=project_points(obj.get("corners",[]),meta)
        if projected is None: continue
        intersection=projected.intersection(target).area
        iou=intersection/projected.union(target).area
        if intersection/target.area>=.8 and .2<=target.area/projected.area<=1.8:
            candidates.append((iou,obj["id"]))
    candidates.sort(reverse=True)
    if not candidates or (len(candidates)>1 and candidates[0][0]-candidates[1][0]<.1): return ""
    return candidates[0][1]


def visual_overlap(track, observation):
    """Use calibrated projection, or near-stationary rotation when depth is absent."""
    meta=observation.get("metadata",{})
    if meta.get("tracking")!="normal" or len(observation.get("polygon",[]))!=4: return 0.
    target=Polygon(observation["polygon"])
    if not target.is_valid or target.area<=0: return 0.
    projected=project_points(track.get("corners",[]),meta)
    scores=[]
    if projected is not None:
        scores.append(projected.intersection(target).area/projected.union(target).area)
    for view in track.get("views",[]):
        old=view["metadata"]
        if old.get("ar_session_id")!=meta.get("ar_session_id") or old.get("tracking")!="normal": continue
        try:
            a=np.asarray(old["camera_transform"]).reshape(4,4,order="F")
            b=np.asarray(meta["camera_transform"]).reshape(4,4,order="F")
            # Without scale, translation cannot be compensated safely. Pure
            # rotation is calibrated; this path never creates metric dimensions.
            if np.linalg.norm(a[:3,3]-b[:3,3])>.025: continue
            ka=np.asarray(old["intrinsics"]).reshape(3,3,order="F")
            kb=np.asarray(meta["intrinsics"]).reshape(3,3,order="F")
            pixels=np.asarray(view["polygon"])*[old["image_width"],old["image_height"]]
            rays=np.column_stack(((pixels[:,0]-ka[0,2])/ka[0,0],-(pixels[:,1]-ka[1,2])/ka[1,1],-np.ones(4)))
            current=(b[:3,:3].T @ a[:3,:3] @ rays.T).T
            if np.any(current[:,2]>=-.1): continue
            xy=np.column_stack((-kb[0,0]*current[:,0]/current[:,2]+kb[0,2],kb[1,1]*current[:,1]/current[:,2]+kb[1,2]))
            xy/=[meta["image_width"],meta["image_height"]]
            p=Polygon(xy).intersection(box(0,0,1,1))
            if p.is_valid and p.area>0: scores.append(p.intersection(target).area/p.union(target).area)
        except (ValueError,KeyError,TypeError,np.linalg.LinAlgError): continue
    return max(scores,default=0.)


class TrackRegistry:
    def __init__(self, tracks=None, correspondence=None):
        self.tracks = tracks if tracks is not None else {}
        self.correspondence = correspondence

    def associate(self, observation: dict) -> dict:
        corners = np.asarray(observation.get("world_corners", []), dtype=float)
        frame = observation["frame_id"]
        kind = observation.get("kind", "book")
        meta=view_metadata(observation.get("metadata",{}))
        session=meta.get("ar_session_id","")
        anchor=observation.get("roomplan_id","")
        support=observation.get("depth_anchor")
        valid=corners.shape==(4,3) and np.isfinite(corners).all()
        reason="" if valid else "Metric association unavailable; possible repeat view requires review"
        candidates=[]
        for id,track in self.tracks.items():
            if track["kind"]!=kind or frame in track["frames"]: continue
            if session and track.get("ar_session_id") and track["ar_session_id"]!=session: continue
            if kind=="item" and track.get("category") and not category_matches(track["category"],observation.get("category","")): continue
            old_anchor=track.get("roomplan_id","")
            if anchor and old_anchor and anchor!=old_anchor: continue
            if anchor and old_anchor==anchor:
                candidates.append((0.,id,"RoomPlan physical object"));continue
            old_support=track.get("depth_anchor")
            if kind=="item" and support and old_support and session and track.get("ar_session_id")==session:
                distance=float(np.linalg.norm(np.array(support["center"])-old_support["center"]))
                tolerance=min(.18,max(.07,.05+.3*max(support["radius_m"],old_support["radius_m"])))
                if distance<tolerance:
                    candidates.append((distance,id,"Observed LiDAR support"))
                # Strong, disagreeing depth takes precedence over overlapping
                # image outlines and extrapolated plane corners.
                continue
            old=np.asarray(track.get("corners",[]))
            if valid and old.shape==(4,3):
                edges=np.linalg.norm(np.roll(corners,-1,axis=0)-corners,axis=1)
                old_edges=np.linalg.norm(np.roll(old,-1,axis=0)-old,axis=1)
                small,large=min(edges),max(edges)
                if min(old_edges)>0 and max(old_edges)>0 and .6<=small/min(old_edges)<=1.7 and .65<=large/max(old_edges)<=1.5:
                    distance=float(np.linalg.norm(corners.mean(axis=0)-old.mean(axis=0)))
                    tolerance=min(.15 if kind!="book" else .045,max(.012,small*.6+.004))
                    if distance<tolerance:
                        candidates.append((distance,id,"LiDAR physical anchor"));continue
                # Two measured surfaces separated in depth must not merge just
                # because their projected silhouettes overlap.
                continue
            if kind=="item" and session and track.get("ar_session_id")==session and category_matches(track.get("category",""),observation.get("category","")):
                score=visual_overlap(track,observation)
                if score>=.55:
                    candidates.append((.15+(1-score)*.2,id,"Calibrated visual overlap"))
                    continue
                if self.correspondence and meta.get("tracking")=="normal":
                    for view in reversed(track.get("views",[])):
                        previous=view["metadata"]
                        if previous.get("tracking")!="normal" or previous.get("ar_session_id")!=session: continue
                        a=np.array(previous["camera_transform"]).reshape(4,4,order="F")
                        b=np.array(meta["camera_transform"]).reshape(4,4,order="F")
                        angle=np.arccos(np.clip((np.trace(a[:3,:3].T@b[:3,:3])-1)/2,-1,1))
                        if np.linalg.norm(a[:3,3]-b[:3,3])>.75 or angle>np.deg2rad(40): continue
                        score=self.correspondence.match_polygon(view["frame_id"],frame,view["polygon"],observation["polygon"])
                        if score>=.55:
                            candidates.append((.25+(1-score)*.2,id,"Verified image correspondence"));break
        anchored=[candidate for candidate in candidates if candidate[2]=="RoomPlan physical object"]
        if anchored: candidates=anchored
        candidates.sort()
        if len(candidates)>1 and candidates[1][0]-candidates[0][0]<(.008 if kind=="book" else .02):
            reason="Ambiguous physical association; possible duplicate/split requires review"
            candidates=[]
        if candidates:
            _,id,method=candidates[0]
            track=self.tracks[id]
            track["frames"].append(frame)
            if not track.get("corners") and valid: track["corners"]=corners.tolist()
            if anchor: track["roomplan_id"]=anchor
            if support and not track.get("depth_anchor"): track["depth_anchor"]=support
            self._view(track,observation,meta)
            return {"id":id,"new":False,"reason":"" if valid else "Repeated view linked; metric dimensions remain unavailable", "association":method}
        id=("b_" if kind=="book" else "s_" if kind=="shelf" else "i_")+uuid4().hex[:12]
        self.tracks[id]={"kind":kind,"corners":corners.tolist() if valid else [],"frames":[frame],
                         "category":observation.get("category",""),"ar_session_id":session,"roomplan_id":anchor,
                         "depth_anchor":support}
        self._view(self.tracks[id],observation,meta)
        return {"id":id,"new":True,"reason":reason,"association":"new observation"}

    @staticmethod
    def _view(track,observation,meta):
        if meta and len(observation.get("polygon",[]))==4:
            views=track.setdefault("views",[])
            views.append({"frame_id":observation["frame_id"],"polygon":observation["polygon"],"metadata":meta})
            del views[:-8]
