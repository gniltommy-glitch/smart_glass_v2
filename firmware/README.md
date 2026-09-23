# STM32F411E-DISCO: tai nghe CS43L22 và nút xanh

Firmware mới: **Pi gửi PCM qua UART → STM32 → I2S3/DMA → CS43L22 → jack CN4 3,5 mm**.
STM32 đồng thời đọc nút xanh PA0 và bật LED đỏ PD14. ToF do Pi đọc trực tiếp.

File nguồn cần sửa/build là **`smart_glass_v2/firmware/main.c`**.
Board yêu cầu STM32F411E-DISCO / MB1115, STM32F411VET6, HSE 8 MHz và codec CS43L22.
Black Pill F411CE không có sẵn codec/jack/LED giống board này.

## 1. Nạp file nào?

- `main.c`: mã nguồn; cần biên dịch cùng startup, linker và thư viện.
- `build/stm32_glass_audio.elf`: chương trình hoàn chỉnh, dùng nạp/debug bằng CubeIDE.
- `build/stm32_glass_audio.hex`: chương trình kèm địa chỉ Flash, dùng CubeProgrammer.
- `build/stm32_glass_audio.bin`: dữ liệu thô, địa chỉ nạp `0x08000000`.

Đây là **giao thức binary v2, UART 1 Mbaud**. Không nạp bản nút ASCII 115200 trước
đó hoặc bản audio protocol v1 ở thư mục cha rồi chạy Python v2 hiện tại.

## 2. Build bằng GCC đi kèm STM32CubeIDE

Mở Terminal tại thư mục gốc `smt32_sum`:

```powershell
python -X utf8 smart_glass_v2/firmware/build.py
```

Script tìm `arm-none-eabi-gcc.exe` dưới `C:/ST`. Nếu IDE ở nơi khác:

```powershell
python -X utf8 smart_glass_v2/firmware/build.py --gcc "D:/duong-dan-IDE/tools/bin/arm-none-eabi-gcc.exe"
```

Thư mục này đã có **CMSIS + HAL + startup + linker script** và giấy phép thư viện.
Build script cấu hình macro `STM32F411xE`, `USE_HAL_DRIVER`, `HSE_VALUE=8000000U`,
include path và các nguồn HAL cần thiết. Không chỉ chép `main.c` vào project rỗng:
làm vậy lại gặp lỗi thiếu `stm32f4xx_hal.h` hoặc thiếu định nghĩa HAL khi link.

## 3. Nạp/debug bằng STM32CubeIDE

1. Cắm USB vào cổng **ST-LINK**; giữ hai jumper ST-LINK nối tới MCU đích.
2. Chạy lệnh build ở trên để tạo ELF.
3. Trong CubeIDE, mở project STM32F411 làm ngữ cảnh debug.
4. **Run → Debug Configurations… → STM32 C/C++ Application**, tạo cấu hình
   `SmartGlass_Audio_V2`.
5. Tab **Main**, `C/C++ Application`: Browse tới đúng
   `smart_glass_v2/firmware/build/stm32_glass_audio.elf`.
   Tắt tự build project trước khi launch trong cấu hình này; ELF đã được build bằng script.
6. Tab **Debugger**: ST-LINK, SWD, target STM32F411VE; thử hardware reset nếu kết nối lỗi.
7. Tab **Startup**: bật Load image và reset; nếu có đường dẫn image riêng thì cũng
   chọn `stm32_glass_audio.elf`.
8. Bấm **Debug** rồi **Resume/F8** nếu IDE dừng tại `main`.
9. LED xanh lá PD12 đổi trạng thái mỗi giây. Nhấn USER xanh làm LED đỏ PD14 sáng.
   Nút RESET màu đen không gửi yêu cầu OCR.

Tên mục có thể khác giữa phiên bản CubeIDE. Cách nạp trực tiếp:
STM32CubeProgrammer → ST-LINK → Connect → chọn `stm32_glass_audio.hex` →
Download/Verify → reset board.

## 4. Nối chân và test tai nghe

| Pi 5, chân vật lý | STM32 |
| --- | --- |
| Pin 8 / GPIO14 TXD0 | PA3 / USART2 RX |
| Pin 10 / GPIO15 RXD0 | PA2 / USART2 TX |
| Pin 9 / GND | GND |

3,3 V TTL, **1.000.000 baud, 8N1**. Nguồn board qua ST-LINK; nguồn Pi riêng; không
nối chéo 5V giữa hai nguồn. Nếu dùng USB-UART, chọn adapter hỗ trợ 1 Mbaud và TTL 3V3.

Tai nghe cắm **jack CN4 trên board STM32**. Codec CS43L22 thực hiện chuyển đổi
số–analog và khuếch đại tai nghe; các dây sau đã có trên PCB:

| Tín hiệu codec | Chân STM32 |
| --- | --- |
| I2C1 SCL / SDA | PB6 / PB9 |
| RESET | PD4 |
| I2S WS / MCLK / CK / SD | PA4 / PC7 / PC10 / PC12 |

Chạy trên Pi, trong `smart_glass_v2`, sau khi bật UART như README chính:

```bash
python diagnose.py audio --tone --duration 10
python diagnose.py audio --duration 60
python diagnose.py audio --wav /duong/dan/baihat.mp3 --duration 240
```

Dòng đầu không cần model, phát bíp 440 Hz nhỏ trong một giây. Dòng thứ hai cần
giọng Piper Việt. Dòng cuối tự chuyển MP3/WAV sang PCM 16000 Hz, mono, signed 16-bit.
Không mở thêm chương trình serial hoặc trợ lý đồng thời. Test thành công bằng
phần mềm mới xác nhận truyền/DMA; cần **nghe thật** để xác nhận jack và tai nghe.

## 5. Nút, LED và tín hiệu âm thanh

| Thao tác/trạng thái | Kết quả |
| --- | --- |
| Nhấn ngắn rồi thả | Nút loại 1 → Pi ánh xạ OCR/xác nhận theo ngữ cảnh |
| Giữ ≥1,2 giây rồi thả | Nút loại 2 → nhận diện tiền, một sự kiện |
| Nhấn hợp lệ sau chống dội 25 ms | LED đỏ PD14 chốt sáng; reset để tắt |
| Hoạt động bình thường | PD12 đổi trạng thái mỗi giây |
| Không khởi tạo codec hoặc lỗi DMA | PD13 cam sáng |
| Không khởi tạo được clock HSE | PD13 cam chớp nhanh |
| Hàng đợi nút đầy | PD15 xanh dương sáng |

LED để người lắp kiểm tra. Người dùng khiếm thị nghe lời khởi động, lời nhắc và
kết quả qua tai nghe. EXTI0 bắt hai cạnh; ISR chỉ tăng bộ đếm, không gửi UART/chờ.
Chống dội và đo thời gian giữ chạy trong `main`.

Clock CPU 96 MHz, APB1 48 MHz, USART2 BRR=48 cho 1 Mbaud. PLLI2S M=8/N=213/R=2;
I2S gần 16 kHz, MCLK gần 4,096 MHz. DMA1 Stream5 Channel0 truyền I2S3 liên tục.
PCM mono nhân đôi ra hai kênh. Master volume CS43L22 khởi tạo -20 dB.

## 6. Giao thức binary v2

```text
A5 5A | type:u8 | sequence:u16 LE | length:u16 LE | payload | CRC16:u16 LE
```

CRC16/CCITT-FALSE: polynomial `0x1021`, init `0xFFFF`, không phản chiếu/xor cuối,
tính từ `type` đến hết `payload`. Payload tối đa 1024 byte.

| Type | Payload |
| --- | --- |
| START 0x10 | play_id:u32 + tổng số mẫu mono:u32 |
| PCM 0x11 | Các mẫu signed 16-bit little-endian, tối đa 512 mẫu/gói |
| STOP 0x12 | Rỗng |
| BUTTON_ACK 0x20 | Bộ đếm nút:u32 |
| ACK 0x80 | Một byte: 0=nhận, 1=ring đầy/bận, 2=gói sai, 3=codec chưa hoạt động |
| BUTTON 0x81 | Bộ đếm:u32 + loại:u8 (1=ngắn, 2=giữ lâu) |
| STATUS 0x83 | 40 byte, layout `<4B9I`, xem `utils/uart_protocol.py` |
| DONE 0x84 | play_id:u32 + số mẫu đã phát:u32 |
| BOOT 0x85 | Rỗng, báo reset |

Chỉ một lệnh audio chờ ACK tại một thời điểm. Mất ACK/ring đầy → thử lại **cùng
sequence**; STM32 bỏ gói trùng để không phát lặp mẫu. Pi ACK nút sau khi queue nhận
được, bỏ sự kiện trùng; STM32 thử lại mỗi 100 ms trong tối đa 2 giây.

Ring PCM 8192 mẫu (~512 ms), UART ring 4096 byte. DMA gồm hai nửa, mỗi nửa 256
frame stereo. STOP bỏ ring nhưng không ghi đè nửa DMA đang chạy, nên phần cuối có
thể còn khoảng 32 ms. Pi chờ 40 ms trước START mới. DONE gửi sau khi mẫu cuối qua
DMA và chờ thêm 34 ms, giúp thời gian xác nhận sang đường tính sau khi phát xong.

Thiếu PCM phát mẫu 0 và tăng `underruns`. Thiếu lệnh audio hơn 1,5 giây trong khi
chưa nhận hết đoạn → tự dừng đoạn dở. ToF không chạy trên STM32 trong bản này.

## 7. Nguồn và phạm vi kiểm tra

Đối chiếu [UM1842 của ST](https://www.st.com/resource/en/user_manual/um1842-discovery-kit-with-stm32f411ve-mcu-stmicroelectronics.pdf),
sơ đồ MB1115 người dùng cung cấp và [driver CS43L22 của ST](https://github.com/STMicroelectronics/stm32-cs43l22).
Đã build ARM GCC và kiểm tra vector; chưa có board để đo I2S, điện áp jack hoặc
nghe âm thanh thật. Xem [kết quả kiểm tra](../VALIDATION.md).
