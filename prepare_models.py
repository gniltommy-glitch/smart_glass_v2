"""Tải model công khai khi người dùng chủ động chạy lệnh cài đặt, không gọi lúc khởi động."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parent

def download(url, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        print("Giữ file đã có:", path)
        return
    temporary = path.with_suffix(path.suffix+".part")
    print("Đang tải:", url)
    try:
        with urllib.request.urlopen(url, timeout=90) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        if temporary.stat().st_size == 0:
            raise ValueError("File tải về rỗng")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)

def prepare_ocr():
    import yaml
    folder = ROOT/"models"/"ocr"
    folder.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.find_spec("rapidocr_onnxruntime")
    if spec is None or spec.origin is None:
        raise RuntimeError("Cài requirements.txt trước khi chuẩn bị OCR")
    package = Path(spec.origin).parent
    package_cfg = yaml.safe_load((package/"config.yaml").read_text(encoding="utf-8"))
    detector = package/package_cfg["Det"]["model_path"]
    if not detector.is_file():
        raise FileNotFoundError(f"Wheel RapidOCR thiếu detector: {detector}")
    if not (folder/"text_detector.onnx").exists():
        shutil.copyfile(detector, folder/"text_detector.onnx")
    for name in ("base.yml", "vgg-seq2seq.yml"):
        download("https://raw.githubusercontent.com/pbcquoc/vietocr/master/config/"+name, folder/name)
    settings = yaml.safe_load((folder/"base.yml").read_text(encoding="utf-8"))
    settings.update(yaml.safe_load((folder/"vgg-seq2seq.yml").read_text(encoding="utf-8")))
    url = settings["weights"]
    if not url.startswith("https://"):
        raise ValueError("Cấu hình nguồn không có URL trọng số HTTPS; kiểm tra lại nguồn VietOCR")
    download(url, folder/"vietocr.pth")
    settings.update(device="cpu", weights="models/ocr/vietocr.pth")
    settings["cnn"]["pretrained"] = False
    settings.setdefault("predictor", {})["beamsearch"] = False
    if not (folder/"vietocr.yml").exists():
        (folder/"vietocr.yml").write_text(yaml.safe_dump(settings, allow_unicode=True, sort_keys=False), encoding="utf-8")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yolo", action="store_true")
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--piper", action="store_true")
    args = parser.parse_args()
    if not any((args.yolo, args.ocr, args.piper)):
        parser.error("Chọn --yolo, --ocr hoặc --piper; có thể chọn cả ba")
    os.environ.setdefault("YOLO_CONFIG_DIR", str(ROOT/".cache"/"ultralytics"))
    if args.yolo:
        destination = ROOT/"models"/"yolo.pt"
        if not destination.exists():
            from ultralytics import YOLO
            source = ROOT/"models"/"yolo11n.pt"
            YOLO(str(source))
            shutil.copyfile(source, destination)
        print("YOLO nano COCO: có người/xe, chưa có lớp mặt đường hoặc vạch sang đường.")
    if args.ocr:
        prepare_ocr()
    if args.piper:
        directory = ROOT/"models"/"piper"
        directory.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, "-m", "piper.download_voices", "vi_VN-vais1000-medium",
                        "--data-dir", str(directory)], check=True)
    manifest = {}
    for path in (ROOT/"models").rglob("*"):
        if path.is_file() and path.name != "sha256.json" and path.suffix in {".pt", ".pth", ".onnx", ".yml", ".json"}:
            with path.open("rb") as source:
                manifest[str(path.relative_to(ROOT))] = hashlib.file_digest(source, "sha256").hexdigest()
    (ROOT/"models"/"sha256.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Đã lưu dấu vân tay file; tiếp theo chạy --check và test OCR/âm thanh thật.")

if __name__ == "__main__":
    main()
