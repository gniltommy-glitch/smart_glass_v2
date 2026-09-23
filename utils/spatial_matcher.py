"""Chiếu vùng giữa bbox vào ToF bằng mô hình camera lỗ kim và FOV đã hiệu chuẩn."""
import math
import numpy as np

def normalize_matrix(raw_mm, cfg):
    raw = np.asarray(raw_mm, dtype=float).reshape(8, 8)
    matrix = np.where(np.isfinite(raw) & (raw >= cfg["min_mm"]) &
                      (raw < cfg["invalid_from_mm"]), raw/1000.0, np.nan)
    matrix = np.rot90(matrix, cfg["rotate_quarters"])
    if cfg["flip_x"]:
        matrix = np.fliplr(matrix)
    if cfg["flip_y"]:
        matrix = np.flipud(matrix)
    return matrix.copy()

def sector(bbox, size):
    width, height = size
    cx, cy = (bbox[0]+bbox[2])/2, (bbox[1]+bbox[3])/2
    row, col = min(2, max(0, int(cy*3/height))), min(2, max(0, int(cx*3/width)))
    return [["trên trái", "trên giữa", "trên phải"],
            ["giữa trái", "chính giữa", "giữa phải"],
            ["dưới trái", "dưới giữa", "dưới phải"]][row][col]

def matching_cells(bbox, size, camera_cfg, tof_cfg):
    width, height = size
    x1, y1, x2, y2 = map(float, bbox)
    if x2 <= x1 or y2 <= y1 or width <= 0 or height <= 0:
        return []
    cx, cy = (x1+x2)/2, (y1+y2)/2
    half_w = max(0.5, (x2-x1)*tof_cfg["center_fraction"]/2)
    half_h = max(0.5, (y2-y1)*tof_cfg["center_fraction"]/2)
    focal = width/(2*math.tan(math.radians(camera_cfg["hfov_deg"])/2))
    yaw, pitch = math.radians(tof_cfg["yaw_deg"]), math.radians(tof_cfg["pitch_deg"])
    # Góc lệch nhỏ được hiệu chỉnh riêng; không tự kẹp vật ngoài FOV vào ô biên.
    xs = [math.tan(math.atan((x-width/2)/focal)-yaw) for x in [cx-half_w, cx+half_w]]
    ys = [math.tan(math.atan((y-height/2)/focal)-pitch) for y in [cy-half_h, cy+half_h]]
    tx, ty = math.tan(math.radians(tof_cfg["hfov_deg"])/2), math.tan(math.radians(tof_cfg["vfov_deg"])/2)
    edges_x, edges_y = np.linspace(-tx, tx, 9), np.linspace(-ty, ty, 9)
    return [(r, c) for r in range(8) for c in range(8)
            if edges_x[c] < xs[1] and edges_x[c+1] > xs[0]
            and edges_y[r] < ys[1] and edges_y[r+1] > ys[0]]

def match_distance(bbox, size, camera_cfg, tof_cfg, sample, frame_time, now):
    if not tof_cfg["calibrated"] or sample is None:
        return None
    if now-sample["time"] > tof_cfg["max_age_s"] or abs(frame_time-sample["time"]) > tof_cfg["fusion_skew_s"]:
        return None
    cells = matching_cells(bbox, size, camera_cfg, tof_cfg)
    values = [sample["matrix"][r, c] for r, c in cells if np.isfinite(sample["matrix"][r, c])]
    return float(np.mean(values)) if values else None
