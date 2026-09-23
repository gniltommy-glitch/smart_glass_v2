# Kết quả kiểm tra — 22/09/2026

## Đã thực hiện trên máy phát triển Windows

- `python -X utf8 -m unittest discover -s smart_glass_v2/tests -v`: **30/30 qua**,
  gồm mô phỏng cả pipeline với nút OCR và thoát sạch.
- Kiểm tra cú pháp toàn bộ Python mới bằng `ast.parse(feature_version=(3, 11))`.
  Runtime thực tế dùng Python 3.12; chưa chạy test bằng interpreter 3.11.9.
- `main_coordinator.py --mock --duration 3 --mock-button-after 1`: chạy và thoát mã 0.
- `diagnose.py` cho ToF, button, camera, audio: chạy được ở chế độ `--mock`.
- ARM GCC đi kèm STM32CubeIDE 2.2.0: build thành công với `-Wall -Wextra -Werror`.
  `text=12196`, `data=12`, `bss=32340` byte; bss tính cả vùng dự phòng heap/stack.
- Đọc vector table trong BIN: stack đầu `0x20020000`; vector Reset, SysTick,
  EXTI0, USART2 và DMA1 Stream5 trỏ đúng các handler. Handler ngắt do chương trình định nghĩa,
  không bị rơi vào weak default handler của startup.
- Các lệnh sửa của lần chuyển audio này chỉ ghi trong `smart_glass_v2/`.
  Đối chiếu snapshot `legacy_sha256.json` từ lần trước: 99/100 file trùng;
  `main_win_test.py` khác snapshot. File đó không được sửa bởi các lệnh của lần này,
  và bản hiện có được giữ lại, không khôi phục đè từ snapshot.

## Các tình huống đã có kiểm thử

CRC sai, gói UART chia đoạn, nút gửi lặp, STM32 reset, queue nút đầy không ACK;
ToF FIFO đọc thành đoạn 32 byte, lỗi lệnh/độ dài/timeout; loại giá trị 4000/NaN;
xoay/lật ma trận; phép ghép ngoài FOV, chưa hiệu chuẩn và dữ liệu quá cũ;
ô gần không bị trung bình cột che khuất; ngưỡng hơn 4 ô và cụm đông đối tượng;
10 frame xe khác nhau, xe tiến gần, camera rung, thiếu xe và khoảng cách frame lớn;
đếm cửa sổ xác nhận từ lúc đọc xong; OCR/tiền theo ngữ cảnh nút; cảnh báo gần
hủy sang đường; queue giới hạn; ưu tiên và ngắt tiến trình âm thanh đang chạy;
OCR crop và lọc confidence; giữ Unicode/số khi làm sạch văn bản; truyền lỗi worker.

Kiểm thử UART audio mới chạy với thiết bị giả: ring đầy trả BUSY, ACK mất, gửi lại
cùng sequence không lặp mẫu PCM; nút được nhận/ACK trong khi đang gửi âm thanh;
DONE sai mã lần phát bị bỏ qua; DONE đúng đến trễ phải được chờ; cảnh báo khẩn gửi
STOP trước khi kết thúc hủy; reset và ACK trong cùng lần đọc vẫn báo lỗi;
WAV sai sample rate bị từ chối trước khi gửi UART; kiểm tra phiên bản/layout STATUS.

## File nạp

| File | SHA256 |
| --- | --- |
| `firmware/build/stm32_glass_audio.hex` | `8fdffb54780173166e238af05a424f9dc83abe2f1df9839502f3bb420e18bd8c` |
| `firmware/build/stm32_glass_audio.bin` | `9f56e114ee813cd0cec9d375d22f65952886c12d89c7af00296b283089ff7f7d` |

## Chưa xác nhận

Chưa có phần cứng trong phiên làm việc: chưa nạp board, chưa đo EXTI/UART/I2C,
chưa nghe tiếng thật qua jack CS43L22 trên STM32 hoặc nhận ảnh IMX708. Chưa tải trọng số,
chưa kiểm tra inference thật hoặc benchmark RAM/FPS/độ trễ trên Pi 5 4GB.
`prepare_models.py` đã kiểm tra cú pháp; chưa thực hiện tải model đầu cuối.
requirements cần cài và kiểm tra trên Pi OS 64-bit của người dùng.
Các bài test dùng dữ liệu giả không chứng minh độ chính xác ngoài đường.
