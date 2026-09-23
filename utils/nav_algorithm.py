"""Tìm cụm cột thoáng; ô mất dữ liệu luôn được coi là chưa biết."""
import numpy as np

def extract_corridor(matrix, cfg):
    matrix = np.asarray(matrix, dtype=float)
    if matrix.shape != (8, 8):
        raise ValueError("Ma trận khoảng cách phải có kích thước 8×8")
    # Quét các hàng giữa và dưới; ô gần trong một cột có quyền chặn cả cột.
    band = matrix[2:8]
    valid = np.isfinite(band) & (band > 0)
    counts = valid.sum(axis=0)
    totals = np.where(valid, band, 0).sum(axis=0)
    means = np.divide(totals, counts, out=np.zeros(8), where=counts > 0)
    minimum = np.min(np.where(valid, band, np.inf), axis=0)
    clear = (counts >= int(np.ceil(6*cfg["min_valid_fraction"]))) & (minimum >= cfg["clear_m"])
    runs, start = [], None
    for c in range(9):
        if c < 8 and clear[c]:
            if start is None:
                start = c
        elif start is not None:
            if c-start >= cfg["min_columns"]:
                runs.append((start, c-1, float(means[start:c].mean())))
            start = None
    if not runs:
        return {"direction": None, "columns": [], "mean_m": None}
    left, right, distance = max(runs, key=lambda run: (run[2], run[1]-run[0], -abs((run[0]+run[1])/2-3.5)))
    center = (left+right)/2
    direction = "left" if center < 2.5 else "right" if center > 4.5 else "straight"
    return {"direction": direction, "columns": list(range(left, right+1)), "mean_m": distance}

def obstacle_summary(matrix, object_count, cfg):
    matrix = np.asarray(matrix, dtype=float)
    close = np.isfinite(matrix) & (matrix > 0) & (matrix < cfg["near_m"])
    triggered = int(close.sum()) > cfg["near_cells"] or object_count >= cfg["crowd_objects"]
    emergency = bool(close[2:7, 3:5].any())
    return {"triggered": triggered, "emergency": emergency, "close_cells": int(close.sum()),
            **extract_corridor(matrix, cfg)}
