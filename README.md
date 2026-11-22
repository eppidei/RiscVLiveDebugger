# Live Memory Tuner, Telemetry & Hardware Breakpoint Dashboard

A standalone, non-intrusive memory inspection, live parameter tuning, hardware breakpoint/watchpoint, and telemetry acquisition GUI for embedded processors and FPGA soft-cores.

While conceived specifically to overcome the limitations of debugging workflows on **Microchip PolarFire SoC and Mi-V (RISC-V)** soft-cores, the architecture connects directly to **OpenOCD's TCL server (port 6666)** and is target-agnostic—functioning with any CPU or core supported by OpenOCD (RISC-V, ARM Cortex-M, ESP32, etc.).

---

## Table of Contents

1. [Motivation: The PolarFire / Mi-V Debugging Problem](#1-motivation-the-polarfire--mi-v-debugging-problem)
2. [Modular Architecture & Repository Structure](#2-modular-architecture--repository-structure)
3. [Firmware Requirements & Best Practices](#3-firmware-requirements--best-practices)
4. [2D Workspace & Widget Reference](#4-2d-workspace--widget-reference)
5. [OpenOCD Integration & Colorized Console](#5-openocd-integration--colorized-console)
6. [Getting Started & Typical Workflow](#6-getting-started--typical-workflow)
7. [Failure Modes, Edge Cases & Limitations](#7-failure-modes-edge-cases--limitations)
8. [Roadmap & Future Improvements](#8-roadmap--future-improvements)
9. [License & Attribution](#9-license--attribution)

---

## 1. Motivation: The PolarFire / Mi-V Debugging Problem

Developing real-time embedded firmware on FPGA soft-cores (such as the Microchip Mi-V core implemented in PolarFire FPGA fabric) reveals significant deficiencies in traditional IDE-based toolchains (e.g., SoftConsole, Eclipse, and GDB):

- **Invasive Halts Destroy Real-Time Systems:** Traditional debugging relies on heavy GDB sessions. Prolonged halts freeze PWM generation, motor commutation, closed-loop PID controllers, and active communication buses, frequently triggering hardware faults.
- **GDB Polling Instability:** Continuous watch-expression polling over GDB often leads to serial/JTAG packet desynchronization, crashing the GDB server and necessitating complete hardware power cycles.
- **Lack of Specialized Instrumentation:** Standard debuggers cannot natively render live time-series plots, decode fixed-point ($Q$-format) fractions, parse hardware bitfield registers dynamically, or plot memory arrays as histograms.
- **Heavyweight Footprint:** Requiring a multi-gigabyte IDE installation just to tune control parameters, inspect buffers, or test hardware in a lab environment introduces unnecessary overhead.

### The Solution

> **Attach-Only Workflow Note:** This dashboard is designed to **attach to an already-programmed/running target** via OpenOCD for live telemetry, parameter tuning, memory block inspection, and breakpoint snapshots—it is **not** a firmware flasher or a full source-level step debugger. Program/flash your firmware image using your normal build/programming flow (e.g., SoftConsole / FlashPro / OpenOCD flash script) first, then load the matching `.map` file (and optional `.lst` file) here to attach and inspect live state.
This tool bypasses GDB entirely. It communicates with OpenOCD via a lightweight TCP/IP socket connection directly to its TCL command interface (port 6666). For live telemetry and tuning, the core is halted, inspected or modified, and resumed within microsecond windows. For deterministic fault-catching, native RISC-V hardware triggers (`bp` and `wp`) halt the target at full clock speed and capture a cycle-coherent snapshot of all firmware variables in a single pass.

---

## 2. Modular Architecture & Repository Structure

The application is split into four focused Python modules for maintainability:

```text
RiscVLiveDebugger/
├── RiscVLiveDebugger.py     # Main GUI window, top toolbars, layout persistence, and workspace
├── ocd_backend.py           # OpenOCD TCP socket client, colorized console launcher, .map & .lst parsers
├── widgets_telemetry.py     # BaseWidget (drag/resize), VarSlider, EnumSelector, ReadMonitor, Plotter, Loader, AliveLight
├── widgets_breakpoints.py   # BaseBreakpointWidget, BreakOnFunc, BreakOnLine, BreakOnVar
├── requirements.txt         # customtkinter, matplotlib
├── README.md
├── LICENSE                  # Apache 2.0
└── NOTICE
```

### System Dataflow

```text
+-----------------------------------------------------------------------+
|          Live Memory Tuner & Breakpoint GUI (CustomTkinter)           |
|  [ReadVar] [WriteSlider] [EnumSwitch] [MemPlotter] [BreakOnFunc/Line] |
+-----------------------------------+-----------------------------------+
                                    |
                  Local TCP Socket (Port 6666 / TCL IPC)
                  * Thread-safe Mutex Lock (self.jtag_lock)
                  * Atomic Halt -> Read/Write -> Resume (Telemetry)
                  * Hardware Trigger -> Full RAM Snapshot (Breakpoints)
                                    |
+-----------------------------------v-----------------------------------+
|                        OpenOCD Server Process                         |
|                 (Colorized PowerShell / Console Stream)               |
+-----------------------------------+-----------------------------------+
                                    |
                      JTAG / SWD Hardware Interface
                      (Microchip FlashPro / FTDI / J-Link)
                                    |
+-----------------------------------v-----------------------------------+
|                    Target System (PolarFire FPGA)                     |
|  +----------------------+             +----------------------------+  |
|  |   Mi-V RISC-V Core   | <---Bus---> |  Uncached LSRAM / DDR RAM  |  |
|  | (Hardware Triggers)  |             |  Mapped via GNU .map/.lst  |  |
|  +----------------------+             +----------------------------+  |
+-----------------------------------------------------------------------+
```

---

## 3. Firmware Requirements & Best Practices

To enable live parameter modification, telemetry acquisition, and function/variable breakpoints without unexpected optimization side effects, structure your C firmware around four core rules:

### 1. Always Declare Monitored & Tuned Variables as `volatile` (Compiler Barrier)
If a variable is not marked `volatile`, GCC's optimization passes (`-O1`, `-O2`, `-O3`, `-Os`) will cache the value inside CPU general-purpose registers (`s0`–`s11`, `a0`–`a7`) across loop iterations and omit physical memory loads or stores.
- **Control variables without `volatile`:** The CPU loads the variable once before entering `while (1)` and never reads RAM again—ignoring GUI slider or choice switcher updates.
- **Monitor variables without `volatile`:** The compiler keeps intermediate counters in registers or eliminates dead stores, leaving stale values in RAM.

```c
/* INCORRECT: Compiler may optimize out memory re-reads */
uint32_t motor_target_rpm = 1000;

/* CORRECT: Forces CPU to emit actual lw/sw instructions on every access */
volatile uint32_t motor_target_rpm = 1000;
```

### 2. Global Linkage for Variables (Symbol Table Resolution)
The GUI resolves variable addresses automatically by parsing the GNU Linker `.map` file.
- Variables must have **global linkage** (declared outside functions without the `static` keyword, or exposed via `-fdata-sections`) so their names and RAM addresses appear in the linker map.
- Do not declare monitored variables as local variables inside a function; local variables live on the stack (`sp`) or in CPU registers and do not have fixed physical RAM addresses.

### 3. When Uncached Memory (LSRAM) Is Strictly Necessary (Hardware Cache Barrier)
On Microchip Mi-V cores, OpenOCD accesses memory when halted by injecting RISC-V load/store instructions (`lw`/`sw`) into the **Debug Program Buffer** and executing them **through the halted CPU's own Load/Store Unit (LSU)**. Because OpenOCD's memory accesses travel through the **exact same L1 Data Cache (D-Cache)** as your firmware:

- **CPU-Only Control Variables (Cached RAM Works):** Global `volatile` variables written by the GUI (`VarSlider`, `EnumSelectorWidget`) and read exclusively by the CPU remain naturally coherent even in cached RAM. OpenOCD writes through the halted CPU's LSU directly into the D-Cache; when the CPU resumes, `volatile` forces a load instruction that hits the updated D-Cache line.
- **FPGA Fabric / DMA Monitor Variables (Uncached LSRAM Mandatory):** If a monitored variable or buffer (e.g., ISP histograms, ADC DMA buffers, frame counters, hardware status words) is updated by **FPGA fabric logic (AXI4 master / DMA)** rather than the CPU, the CPU's D-Cache will hold stale lines. Placing those buffers in **uncached LSRAM** (typically mapped at `0x60000000` on PolarFire Mi-V designs) forces the CPU's LSU to bypass the D-Cache and read physical memory directly.
- **Deterministic Cache Footprint for Large Dumps:** Placing large arrays read by the **Memory Block Plotter** into uncached LSRAM prevents JTAG block dumps from evicting time-critical firmware data out of the D-Cache (cache thrashing).

```c
/* Place fabric-shared or large telemetry buffers in uncached LSRAM */
__attribute__((section(".uncached_lsram"))) volatile uint32_t telemetry_histogram[64];
```

```ld
/* In your linker script (.ld): */
MEMORY
{
    DDR_CACHED (rwx)    : ORIGIN = 0x80000000, LENGTH = 256M
    LSRAM_UNCACHED (rw) : ORIGIN = 0x60000000, LENGTH = 64K
}

SECTIONS
{
    .uncached_lsram (NOLOAD) :
    {
        *(.uncached_lsram*)
    } > LSRAM_UNCACHED
}
```

### 4. Making C Functions Visible in `Break on Func` (Global Linkage & `noinline`)
The **Break on Func** widget populates its scrollable function list by parsing code sections (`.text`, `.ram_code`, `.itim`) in the `.map` file. For a C function to appear cleanly in the list and have a valid hardware breakpoint entry address:

1. **Use Global Linkage (Avoid `static`):**
   In standard C, `static` functions have internal file-scope linkage and are not exported to the global symbol table in the `.map` file. *(Note: While `ocd_backend.py` also extracts `.text.<func_name>` subsections when `-ffunction-sections` is enabled in GCC, `static` functions are still subject to compiler name-mangling such as `my_func.constprop.0` or `my_func.isra.0`).*
2. **Prevent Compiler Inlining (`__attribute__((noinline))`):**
   Under `-O1`, `-O2`, `-O3`, or `-Os`, GCC automatically inlines small functions or functions called only once (`-finline-functions-called-once`) directly into the calling function. Once a function is inlined, it no longer has a standalone entry address in the binary and disappears from the `.map` file completely.
3. **Prevent Dead-Code Stripping (`__attribute__((used))`):**
   If you have diagnostic or fault-handler functions that are not yet called elsewhere, `--gc-sections` will strip them at link time unless marked `used`.

```c
/* Define a clean macro in a shared header */
#ifdef ENABLE_LIVE_TUNING
    /* Debug Build: Global linkage + never inlined -> guaranteed to appear in 'Break on Func' */
    #define DEBUG_FUNC __attribute__((noinline, used))
#else
    /* Release Build: Allow static linkage and full compiler inlining */
    #define DEBUG_FUNC static inline
#endif

/* Usage in your firmware: */
DEBUG_FUNC void PID_Compute_Step(void)
{
    /* Function body is guaranteed to have a discrete entry address in the .map file */
}
```

#### Bonus: Exporting Arbitrary C Lines to the `.map` File Without a `.lst` File
If you want to halt on a specific line inside a large function without loading a `.lst` disassembly file, you can drop a zero-overhead global assembly label onto any C line. Because the label is declared `.global` inside the `.text` section, it automatically appears in the **Break on Func** list:

```c
#define BP_TAG(name) __asm__ volatile (".global " #name "\n" #name ":\n")

void Motor_Control_Loop(void)
{
    Read_Current_Sensors();

    BP_TAG(bp_after_sensors);  /* Appears directly in 'Break on Func' scrollable list! */

    Update_PWM_Duty();
}
```

### 5. Isolate Instrumentation with Build-Controlled Preprocessor Guards
To keep production binaries completely free of debug overhead:
1. In SoftConsole, open **Project Properties ➔ C/C++ Build ➔ Settings ➔ GNU RISC-V Cross C Compiler ➔ Preprocessor**.
2. Add `ENABLE_LIVE_TUNING` to **Defined symbols (`-D`)** for your `Debug` configuration, and omit it from your `Release` configuration.

---

## 4. 2D Workspace & Widget Reference

### Free-Form Drag, Drop & Resize Workspace
Every widget on the dashboard is a movable, resizable 2D tile snapped to a `20 px` alignment grid:
- **Move Tile (`⠿` Top-Left Handle):** Click and drag the `⠿` icon (or the widget border) to position widgets side-by-side or stack compact indicators next to large plots.
- **Resize Tile (`◢` Bottom-Right Grip):** Drag the bottom-right corner to resize any widget horizontally or vertically. Internal plots, sliders, and scrollable lists scale automatically.
- **Save / Load Layout (`layout.json`):** Saves all active widgets, their configured symbols/parameters, and their exact `(x, y, width, height)` geometry coordinates.

---

### Live Telemetry & Control Widgets (`widgets_telemetry.py`)

#### 1. Variable Read Monitor (`+ Read Var`)
- **Fixed-Point Scaling (`Frac Bits`):** Converts raw integer representations of fixed-point numbers ($Q$-format) into engineering units:
  $$\text{Displayed Value} = \frac{\text{Raw Integer}}{2^{\text{Frac Bits}}}$$
- **Bitfield Inspector (`Bitfields`):** Expands up to four configurable bit slices using `high:low` notation (e.g., `7:4`) or single bits (e.g., `2`), displaying both decimal and hex values.
- **Live Time-Series Plot (`Plot`):** Embedded matplotlib trace with adjustable sample buffer depth and manual or auto Y-axis scaling.

#### 2. Variable Write Slider (`+ Write Slider`)
- Continuous parameter tuning for `uint8`, `uint16`, `uint32`, `int8`, `int16`, and `int32` targets.
- Debounced by `200 ms` to avoid saturating the JTAG scan chain while dragging.

#### 3. Discrete Choice / Enum Switcher (`+ Enum Switch`)
- Switches a target variable between discrete states with a single click—no C `enum` definition required in firmware.
- Supports `uint8`, `uint16`, `uint32`, `int8`, `int16`, `int32`, and IEEE-754 `float`.
- Configure label-to-value pairs as `Label1:Val1, Label2:Val2` (e.g., `OFF:0, ON:1` or `10Hz:10.0, 50Hz:50.0, 100Hz:100.0`).

#### 4. Memory Block Plotter (`+ Mem Plotter`)
- Dumps contiguous blocks of `8-bit`, `16-bit`, or `32-bit` words (`mdb`, `mdh`, `mdw`) and renders them as a **Bar (Histogram)** or **Line (Waveform)** graph.

#### 5. Memory File Loader (`+ Mem Loader`)
- Batch-uploads `.hex`, `.dat`, or `.txt` datasets into target RAM in 30-word chunks (ideal for LUTs, FIR filter coefficients, or synthetic test vectors).

#### 6. Alive Light Indicator (`+ Alive Light`)
- High-visibility status indicator: **Green (`TRUE`)** when the variable is non-zero, **Gray (`FALSE`)** when zero, and **Red (`ERROR`)** on JTAG/symbol errors.

---

### Hardware Breakpoint & Watchpoint Widgets (`widgets_breakpoints.py`)

All three breakpoint widgets use the RISC-V Debug Trigger Module (`hw` triggers) and share a unified **Split-Pane Freeze-Frame Architecture**:
- **Left Scrollable Pane:** Searchable, click-to-select list of target functions, source lines, or watch variables.
- **Right Scrollable Pane (Variables Snapshot on Hit):** When the breakpoint triggers, the GUI automatically **disables `Auto Poll` on all other widgets** (preventing accidental target resumption), queries `reg pc`, and batch-reads every RAM variable from the `.map` file.
- **Instant Format Switching:** Switch numerical formats (**`Hex`**, **`uint32`**, **`int32`**, **`uint16`**, **`int16`**, **`uint8`**, **`int8`**, **`float`**, **`bin`**) globally or per variable row without re-reading JTAG.
- **One-Click Resume (`Resume Code`):** Temporarily clears the hardware trigger, single-steps (`step`) past the instruction, re-arms the trigger (if **Keep Armed on Resume** is checked), and resumes execution.

| Widget | Trigger Command | Target Source |
| :--- | :--- | :--- |
| **`+ Break on Func`** | `bp <addr> 2 hw` | Scroll & click any function parsed from `.map` (or enter a raw hex address). |
| **`+ Break on Line`** | `bp <addr> 2 hw` | Scroll & click any `file.c:line` parsed from the optional `.lst` disassembly file (or enter a raw hex address). |
| **`+ Break on Var`** | `wp <addr> <len> <r\|w\|a>` | Scroll & click any global variable from `.map` to halt on memory **Write (`w`)**, **Read (`r`)**, or **Access (`a`)**. |

---

## 5. OpenOCD Integration & Colorized Console

### Colorized Live Console
When clicking **Start OpenOCD**, `ocd_backend.py` launches OpenOCD with `-s <config_dir> -f <main_cfg>` and pipes `stderr`/`stdout` through a real-time color filter:
- **`Error:`** lines highlight in **Red**
- **`Warn :`** lines highlight in **Yellow**
- **`Info :`** lines highlight in **Cyan**
- Routine debug state lines print in **Gray**

### Target Reset (`reset run`)
Clicking **Reset CPU** dispatches `rbp all; rwp all; reset run` over port 6666, clearing active hardware triggers and rebooting the processor cleanly.

---

## 6. Getting Started & Typical Workflow

### Prerequisites
1. **Python 3.8+** installed.
2. Install required dependencies:
   ```bash
   pip install customtkinter matplotlib
   ```
3. **OpenOCD Binary & Drivers:** Ensure OpenOCD is installed (bundled with SoftConsole or standalone) along with the **Microchip FlashPro Express** USB/JTAG drivers.

### Launching the Dashboard
```bash
python RiscVLiveDebugger.py
```

1. Browse to `openocd.exe` and your target `.cfg` file, then click **Start OpenOCD**.
2. Click **Upload .map File** and select your compiled firmware's `.map` file.
3. *(Optional)* Click **Upload .lst File (Opt)** and select your project's `.lst` disassembly file if you plan to use **Break on Line**.
4. Add telemetry, control, or breakpoint widgets from the toolbar, arrange and resize them on the 2D canvas, and click **Save Layout** to persist your workspace.

---

## 7. Failure Modes, Edge Cases & Limitations

1. **Hardware Watchdog Timers (WDT):** Halting a core on a breakpoint freezes software watchdog kick loops. Disable the hardware WDT during breakpoint debugging or service it via an independent fabric timer.
2. **RISC-V Hardware Trigger Count:** Microchip `MIV_RV32` cores synthesize a fixed number of hardware trigger registers (`tdata1`/`tdata2`, typically 2 to 4). Avoid arming more simultaneous hardware breakpoints/watchpoints than your core supports.
3. **Zombie OpenOCD Processes:** Closing the GUI via the window close button (`X`) automatically terminates the OpenOCD process tree (`taskkill /F /T`). If Python is killed forcibly, run `taskkill /F /IM openocd.exe` before restarting.

---

## 8. Roadmap & Future Improvements

### 1. Non-Halting Mode via System Bus Access (SBA)
In the **RISC-V External Debug Support Specification**, a Debug Module can access target memory either via **CPU-Mediated Program Buffer execution** or via a dedicated **System Bus Access (SBA)** master:

| Characteristic | CPU-Mediated Access (Current) | System Bus Access (SBA) |
| :--- | :--- | :--- |
| **Hardware Mechanism** | Injects `lw`/`sw` instructions executed by the halted RISC-V core's LSU. | Dedicated bus master inside the Debug Module connected directly to AXI4 / AHB / TileLink. |
| **Core Halt Required?** | **Yes** (`halt; wait_halt` ➔ `op` ➔ `resume`). | **No.** Reads and writes occur while the CPU runs at full speed. |
| **D-Cache Coherency** | **Coherent** with the CPU's L1 D-Cache (traverses the core's LSU). | **Non-coherent** (bypasses L1 D-Cache; sees physical RAM/bus only). |
| **Uncached Memory Rule** | Mandatory only for FPGA fabric / DMA-shared data. | **Strictly mandatory for all variables** (cached CPU writes are invisible to SBA). |
| **PolarFire Mi-V Support** | **Supported** (`MIV_RV32` / `MIV_ESS`). | **Not implemented** (omitted to conserve FPGA LUT/fabric resources). |

Adding an optional **"Non-Halting (SBA) Mode"** toggle is planned for third-party RISC-V cores that implement SBA and ARM Cortex-M targets using AHB-AP/AXI-AP.

### 2. Multi-Core Target Selector (PolarFire SoC MSS)
Adding a per-widget or global **Core Target Selector (`targets riscv.cpu0` – `riscv.cpu4`)** and multi-`.map` association to support Asymmetric Multiprocessing (AMP) and Symmetric Multiprocessing (SMP) clusters on the 5-core PolarFire SoC.

---

## 9. License & Attribution

This project is licensed under the **Apache License, Version 2.0**. You may freely use, modify, and distribute this software for personal, academic, or commercial purposes.

**Developed by Leonardo Di Carlo.** See the accompanying `NOTICE` file for copyright and attribution details.