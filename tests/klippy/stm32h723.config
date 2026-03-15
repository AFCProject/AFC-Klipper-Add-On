#
# Klipper firmware build configuration for STM32H723 (AFC Lite / BTT boards).
# Requires arm-none-eabi-gcc cross-compiler.
# Used to generate test/dict/stm32h723.dict for AFC integration tests.
#
# To regenerate the dict manually:
#   cp test/klippy/stm32h723.config klipper/.config
#   make -C klipper
#   cp klipper/out/klipper.dict test/dict/stm32h723.dict
#
CONFIG_LOW_LEVEL_OPTIONS=y
CONFIG_MACH_STM32=y
CONFIG_MACH_STM32H723=y
CONFIG_STM32_CLOCK_REF_25M=y
CONFIG_STM32_FLASH_START_20000=y
CONFIG_USBSERIAL=y
CONFIG_STM32_USB_PA11_PA12=y
