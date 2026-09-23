# Mô hình cục bộ

Đặt các model thật ở đây; chương trình không tự tải trọng số khi khởi động.

```text
models/yolo.pt
models/ocr/text_detector.onnx
models/ocr/vietocr.yml
models/ocr/vietocr.pth
models/piper/vi_VN-vais1000-medium.onnx
models/piper/vi_VN-vais1000-medium.onnx.json
models/currency.pt                            # tùy chọn
```

`vietocr.yml` phải là cấu hình đã gộp base.yml và cấu hình kiến trúc khớp trọng số.
Detector dùng DBNet ONNX tương thích RapidOCR 1.3.24; VietOCR nhận từng dòng đã cắt.
Model tiền có thể là YOLO classify hoặc detect; tên class là `vnd_1000` … `vnd_500000`.
Không có model tiền thì chức năng trả lời rõ chưa cài, không đoán mệnh giá.

YOLO nano COCO có người/xe nhưng không có `street`, `sidewalk`, `zebra_crossing`.
Các chức năng mặt đường/vạch sang đường cần model tùy biến có những nhãn đó.
`best.pt` ở thư mục cha được tài liệu dự án ghi là YOLO26 segmentation, không phải
YOLOv8n: nếu dùng nó, cần Ultralytics tương thích với phiên bản huấn luyện 8.4.138.
Đổi tên nhãn trong config không tạo thêm khả năng nhận dạng.
