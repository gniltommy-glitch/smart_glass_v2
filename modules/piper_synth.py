"""Tiến trình con tổng hợp WAV; luồng điều phối có thể ngắt khi cảnh báo khẩn tới."""
import argparse
import json
from pathlib import Path
import wave

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--wav", required=True)
    args = parser.parse_args()
    import onnxruntime as ort
    from piper import PiperVoice
    from piper.config import PiperConfig
    options = ort.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    config = PiperConfig.from_dict(json.loads(Path(args.model+".json").read_text(encoding="utf-8")))
    voice = PiperVoice(config=config, session=ort.InferenceSession(args.model,
                       sess_options=options, providers=["CPUExecutionProvider"]))
    with wave.open(args.wav, "wb") as wav:
        voice.synthesize_wav(Path(args.text).read_text(encoding="utf-8"), wav)

if __name__ == "__main__":
    main()
