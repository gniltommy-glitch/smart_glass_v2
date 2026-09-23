"""Biên dịch firmware nút và âm thanh CS43L22; mọi đầu ra nằm trong thư mục mới."""
import argparse
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gcc", help="Đường dẫn arm-none-eabi-gcc")
    args = parser.parse_args()
    gcc = args.gcc or shutil.which("arm-none-eabi-gcc")
    if not gcc:
        gcc = next(iter(Path("C:/ST").glob("**/arm-none-eabi-gcc.exe")), None)
    if not gcc:
        raise SystemExit("Chưa tìm thấy ARM GCC; truyền --gcc kèm đường dẫn trong STM32CubeIDE")
    gcc = Path(gcc)
    out = ROOT/"build"
    out.mkdir(exist_ok=True)
    flags = ["-mcpu=cortex-m4", "-mthumb", "-mfloat-abi=soft", "-DSTM32F411xE", "-Os", "-g3",
             "-ffunction-sections", "-fdata-sections", "-Wall", "-Wextra", "-std=c11"]
    flags += ["-DUSE_HAL_DRIVER", "-DHSE_VALUE=8000000U"]
    for include in (".", "vendor/core", "vendor/device/Include", "vendor/hal/Inc"):
        flags += ["-I", str(ROOT/include)]
    sources = [ROOT/"main.c", ROOT/"vendor/device/system_stm32f4xx.c",
               ROOT/"vendor/device/gcc/startup_stm32f411xe.s"]
    sources += sorted((ROOT/"vendor/hal/Src").glob("*.c"))
    objects = []
    for source in sources:
        obj = out/(source.stem+".o")
        extra = ["-Werror"] if source.name == "main.c" else ["-Wno-unused-parameter"]
        subprocess.run([str(gcc), *flags, *extra, "-c", str(source), "-o", str(obj)], check=True)
        objects.append(str(obj))
    elf = out/"stm32_glass_audio.elf"
    subprocess.run([str(gcc), *flags, *objects, "-T", str(ROOT/"STM32F411VE.ld"),
                    "--specs=nano.specs", "--specs=nosys.specs", "-nostartfiles",
                    "-Wl,--gc-sections", "-Wl,-Map="+str(out/"stm32_glass_audio.map"), "-o", str(elf)], check=True)
    for fmt, ext in [("binary", "bin"), ("ihex", "hex")]:
        subprocess.run([str(gcc.with_name("arm-none-eabi-objcopy"+gcc.suffix)), "-O", fmt,
                        str(elf), str(elf.with_suffix("."+ext))], check=True)
    subprocess.run([str(gcc.with_name("arm-none-eabi-size"+gcc.suffix)), str(elf)], check=True)
    print("Đã tạo:", elf)

if __name__ == "__main__":
    main()
