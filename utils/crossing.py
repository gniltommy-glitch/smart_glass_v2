"""Theo dõi chuyển động ảnh; không suy ra quyền sang đường từ bbox đứng yên."""
import math

def iou(a, b):
    overlap = max(0, min(a[2], b[2])-max(a[0], b[0]))*max(0, min(a[3], b[3])-max(a[1], b[1]))
    area = (a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-overlap
    return overlap/area if area > 0 else 0.0

def new_observation():
    return {"frames": 0, "last_id": None, "last_time": None, "previous": [],
            "origins": [], "moving": False, "uncertain": False}

def update_observation(state, scene, cfg):
    if scene["id"] == state["last_id"]:
        return None
    vehicles = scene["vehicles"]
    previous = state["previous"]
    dt = scene["time"]-state["last_time"] if state["last_time"] is not None else None
    motion = scene.get("camera_motion")
    if not vehicles or motion is None or motion > cfg["max_camera_motion_px"]:
        state["uncertain"] = True
    if dt is not None and (dt <= 0 or dt > cfg["max_gap_s"]):
        state["uncertain"] = True
    if state["frames"] == 0:
        state["origins"] = vehicles
    elif len(vehicles) != len(previous) or not vehicles:
        state["uncertain"] = True
    else:
        remaining, ordered, origins = list(vehicles), [], []
        for index, old in enumerate(previous):
            candidates = [(iou(old["bbox"], new["bbox"]), j) for j, new in enumerate(remaining)
                          if new["label"] == old["label"]]
            if not candidates or max(candidates)[0] < 0.3:
                state["uncertain"] = True
                break
            _, j = max(candidates)
            new = remaining.pop(j)
            a, b = old["bbox"], new["bbox"]
            origin = state["origins"][index] if index < len(state["origins"]) else old
            def center(box):
                return ((box[0]+box[2])/2, (box[1]+box[3])/2)
            step = math.dist(center(a), center(b))
            travel = math.dist(center(origin["bbox"]), center(b))
            area_a = max(1e-9, (a[2]-a[0])*(a[3]-a[1]))
            area_b = (b[2]-b[0])*(b[3]-b[1])
            o = origin["bbox"]
            area_origin = max(1e-9, (o[2]-o[0])*(o[3]-o[1]))
            if (dt and dt > 0 and step/dt > cfg["speed_norm_s"]) or travel > cfg["total_motion_norm"] or abs(area_b/area_a-1) > cfg["scale_change"] or abs(area_b/area_origin-1) > cfg["scale_change"]:
                state["moving"] = True
            ordered.append(new)
            origins.append(origin)
        if len(ordered) == len(vehicles):
            vehicles, state["origins"] = ordered, origins
    state.update(frames=state["frames"]+1, previous=vehicles,
                 last_id=scene["id"], last_time=scene["time"])
    if state["frames"] < cfg["frames"]:
        return None
    return "moving" if state["moving"] else "uncertain" if state["uncertain"] else "apparently_still"
