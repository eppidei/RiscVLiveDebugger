# Live Memory Tuner & Telemetry Dashboard

A standalone, non-intrusive memory inspection, live parameter tuning, and telemetry acquisition GUI for embedded processors and FPGA soft-cores.

While conceived specifically to overcome the limitations of debugging workflows on **Microchip PolarFire SoC and Mi-V (RISC-V)** soft-cores, the architecture connects directly to **OpenOCD's TCL server (port 6666)** and is target-agnostic—functioning with any CPU or core supported by OpenOCD (ARM Cortex-M, RISC-V, ESP32, etc.).

---

## Table of Contents

1. [Motivation: The PolarFire / Mi-V Debugging Problem](#1-motivation-the-polarfire--mi-v-debugging-problem)
2. [Architectural Overview](#2-architectural-overview)
3. [Firmware Requirements & Best Practices](#3-firmware-requirements--best-practices)
4. [Widget Reference & Field Configuration](#4-widget-reference--field-configuration)
5. [OpenOCD Integration & Target Control](#5-openocd-integration--target-control)
6. [Getting Started & Workflow](#6-getting-started--workflow)
7. [Failure Modes, Edge Cases & Limitations](#7-failure-modes-edge-cases--limitations)
8. [Roadmap & Future Improvements](#8-roadmap--future-improvements)
9. [License & Attribution](#9-license--attribution)

---

## 1. Motivation: The PolarFire / Mi-V Debugging Problem

Developing real-time embedded firmware on FPGA soft-cores (such as the Microchip Mi-V core implemented in PolarFire FPGA fabric) reveals significant deficiencies in traditional IDE-based toolchains (e.g., SoftConsole, Eclipse, and GDB):

- **Invasive Halts Destroy Real-Time Systems:** Traditional debugging relies on hardware or software breakpoints. Halting the CPU freezes PWM generation, motor commutation, closed-loop PID controllers, and active communication buses (SPI, CAN, Ethernet), frequently triggering hardware faults or destructive over-current events.
- **GDB Polling Instability:** Continuous watch-expression polling over GDB often leads to serial/JTAG communication packet desynchronization, crashing the GDB server and necessitating complete hardware power cycles.
- **Lack of Specialized Instrumentation:** Standard debuggers cannot natively render live time-series plots, decode fixed-point ($Q$-format) fractions, parse hardware bitfield registers dynamically, or plot memory arrays as histograms.
- **Heavyweight Footprint:** Requiring a multi-gigabyte IDE installation just to tune control parameters, inspect buffers, or test hardware in a production or lab environment introduces unnecessary overhead.

### The Solution

This tool bypasses GDB entirely. It communicates with OpenOCD via a lightweight TCP/IP socket connection directly to its TCL command interface (port 6666). The core is halted, inspected or modified, and resumed within microsecond windows, providing real-time data acquisition without destabilizing control algorithms or requiring an active IDE session.

---

## 2. Architectural Overview

```text
+-------------------------------------------------------------------+
|                Live Memory Tuner GUI (CustomTkinter)                |
|  [VarSlider]   [ReadMonitor]   [MemPlotter]   [AliveLight]        |
+---------------------------------+---------------------------------+
                                  |
                Local TCP Socket (Port 6666 / TCL IPC)
                * Atomic "Connect -> Halt -> Op -> Resume -> Close"
                                  |
+---------------------------------v---------------------------------+
|                        OpenOCD Server Process                       |
|                   (Embedded or Standalone Instance)                 |
+---------------------------------+---------------------------------+
                                  |
                    JTAG / SWD Hardware Interface
                    (Microchip FlashPro / FTDI / J-Link)
                                  |
+---------------------------------v---------------------------------+
|                    Target System (PolarFire FPGA)                   |
|  +--------------------+             +--------------------------+  |
|  |   Mi-V RISC-V Core | <---Bus---> |      Uncached LSRAM      |  |
|  | (Running Firmware) |             |    Mapped at 0x60000000  |  |
|  +--------------------+             +--------------------------+  |
+-------------------------------------------------------------------+
```

### The 3-Step Atomic Sequence

To avoid debug bus collisions, target panics, or race conditions with the running firmware, all transactions execute an atomic 3-step sequence:

```text
+-------------------+      +----------------------+      +----------+
|  halt; wait_halt  | ---> | read / write command | ---> |  resume  |
+-------------------+      +----------------------+      +----------+
```

Because OpenOCD handles this at the hardware level via the RISC-V Debug Program Buffer, the core halts only for the handful of clock cycles required to service the debug bus request, making it virtually transparent to human perception and real-time loops.

### Connection-Per-Command Strategy

Rather than maintaining a single persistent TCP socket—which can corrupt data frames when multiple asynchronous polling threads collide or when the hardware is reset—every read/write operation opens a local TCP connection, executes its command, and closes the connection. Operating across `localhost`, the connection overhead is less than `0.1 ms`, ensuring complete thread safety and auto-recovery from hardware resets.

---

## 3. Firmware Requirements & Best Practices

To enable live parameter modification and telemetry acquisition without unexpected behavior, firmware running on the target should separate **compiler visibility** (`volatile`) from **hardware cache coherency** (uncached memory).

### 1. Always Declare Monitored & Tuned Variables as `volatile` (Compiler Barrier)
If a variable is not marked `volatile`, GCC's optimization passes (`-O2`, `-O3`) will keep the value in internal processor registers (`a0`–`a7`, `s0`–`s11`) across loop iterations and omit physical memory loads or stores.
- **Control variables without `volatile`:** The CPU loads the variable once before entering `while (1)` and never reads RAM again—ignoring GUI slider updates.
- **Monitor variables without `volatile`:** The compiler keeps intermediate counters in registers or eliminates dead stores, leaving stale values in RAM.

```c
/* INCORRECT: The compiler may optimize out memory re-reads */
uint32_t motor_target_rpm = 1000;

/* CORRECT: Forces the CPU to emit actual load/store instructions every access */
volatile uint32_t motor_target_rpm = 1000;
```

### 2. Global vs. Static Linkage (Symbol Table Resolution)
The GUI resolves variable addresses by parsing the GNU Linker `.map` file.
- Variables must have **global linkage** so their names appear cleanly in the symbol table.
- Avoid declaring monitored variables as `static` inside local function blocks, as the linker will either strip the symbol name or assign a mangled, non-unique identifier.

```c
/* In telemetry.c */
volatile float pid_kp = 1.25f;         /* Visible in .map file */
volatile uint32_t system_status = 0;   /* Visible in .map file */
```

### 3. When Uncached Memory (LSRAM) Is Strictly Necessary (Hardware Cache Barrier)
On Microchip Mi-V cores, OpenOCD does not have an independent hardware bus master to read RAM. Instead, when the core is halted, OpenOCD injects RISC-V load/store instructions (`lw`/`sw`) into the **Debug Program Buffer** and executes them **through the halted CPU's own Load/Store Unit (LSU)**.

Because OpenOCD's memory accesses travel through the **exact same L1 Data Cache (D-Cache)** as your firmware, caching requirements depend on *who* writes the data:

- **CPU-Only Control Variables (Cached RAM Works):** Global `volatile` variables written by the GUI and read exclusively by the CPU remain naturally coherent even in cached RAM. OpenOCD writes through the halted CPU's LSU directly into the D-Cache; when the CPU resumes, `volatile` forces a load instruction that hits the updated D-Cache line.
- **FPGA Fabric / DMA Monitor Variables (Uncached LSRAM Mandatory):** If a monitored variable or array (such as ADC samples, ISP histograms, frame counters, or hardware status words) is updated by **FPGA fabric logic (AXI4 master / DMA)** rather than the CPU, the D-Cache will hold stale lines. Placing those buffers in **uncached LSRAM** (typically mapped at `0x60000000` on PolarFire Mi-V designs) forces the CPU's LSU to bypass the D-Cache and read physical memory directly.
- **Deterministic Cache Footprint for Large Dumps:** Even for CPU-owned arrays, placing large buffers read by the **Memory Block Plotter** into uncached LSRAM prevents JTAG block dumps from evicting time-critical firmware data out of the D-Cache (cache thrashing).

```c
/* In your firmware: place fabric-shared or large telemetry buffers in uncached LSRAM */
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

### 4. Isolate Instrumentation with Build-Controlled Preprocessor Guards
Because `volatile` variables prevent register caching and uncached LSRAM is a scarce FPGA resource, wrap your live-tuning variables and telemetry buffers in a preprocessor macro controlled by your SoftConsole build configuration (`Debug` vs. `Release`).

1. In SoftConsole, open **Project Properties ➔ C/C++ Build ➔ Settings ➔ GNU RISC-V Cross C Compiler ➔ Preprocessor**.
2. Add `ENABLE_LIVE_TUNING` to **Defined symbols (`-D`)** for your `Debug` configuration, and omit it from your `Release` configuration.

```c
#ifdef ENABLE_LIVE_TUNING
    /* Debug / Bench Build: Global, volatile, and mapped for live GUI inspection */
    volatile uint32_t motor_target_rpm = 1000;
    __attribute__((section(".uncached_lsram"))) volatile uint32_t telemetry_histogram[64];
    #define TELEMETRY_RECORD(idx, val) do { telemetry_histogram[(idx)] = (val); } while (0)
#else
    /* Production Release Build: Zero LSRAM footprint and full compiler optimization */
    #define motor_target_rpm (1000U)
    #define TELEMETRY_RECORD(idx, val) ((void)0)
#endif
```

This guarantees that production binaries compile with zero instrumentation overhead and no dangling debug symbols in the `.map` file.

---

## 4. Widget Reference & Field Configuration

### Top Control Bar
- **Upload .map File:** Selects the GNU Linker map file produced during firmware build. Once loaded, all global variable names can be used directly across all widgets in place of hex addresses.
- **OpenOCD Exe & Main CFG:** Paths pointing to the local `openocd.exe` binary and the board initialization `.cfg` script.
- **Start / Stop OpenOCD:** Spawns a dedicated console window hosting OpenOCD with the working directory configured to find nested configuration dependencies.
- **Reset CPU:** Dispatches `reset run` over port 6666, forcing a clean hardware reboot without setting GDB breakpoints.
- **Save / Load Layout:** Exports or imports the current dashboard configuration to a `.json` file.

---

### 1. Variable Write Slider (`+ Write Slider`)
Designed for non-intrusive live parameter adjustment (e.g., motor speed targets, PID gains, state transitions).

| Field | Description |
| :--- | :--- |
| **Addr/Name** | Variable name (e.g., `motor_speed`) or raw hex address (e.g., `0x60000010`). |
| **Type** | Data word width and signedness: `uint8`, `uint16`, `uint32`, `int8`, `int16`, `int32`. |
| **Min / Max** | Integer range limits applied to the slider track. Click **Apply** to commit changes. |
| **Target Value** | Real-time numeric feedback showing the pending value before and during hardware writes. |

*Writes are debounced by 200 ms to avoid flooding the JTAG adapter while dragging.*

---

### 2. Variable Read Monitor (`+ Read Var`)
A comprehensive telemetry node featuring fixed-point conversion, bitfield parsing, and time-series plotting.

- **Fixed-Point Scaling (Frac Bits):** Converts raw integer representation of fixed-point arithmetic ($Q$-format) to real engineering units:

  $$\text{Displayed Value} = \frac{\text{Raw Integer}}{2^{\text{Frac Bits}}}$$

- **Bitfield Inspector:** Expands up to four discrete sub-fields within the word. Enter bit slices using `high:low` notation (e.g., `7:4`) or single bits (e.g., `2`). Automatically displays unsigned values and hexadecimal representations.
- **Time-Series Plotting:** Real-time matplotlib line graph with configurable sample buffer sizes (default 100), manual or automated Y-axis scaling, and a buffer reset function.
- **Auto-Polling:** Polling interval configurable down to 50 ms.

---

### 3. Memory Block Plotter (`+ Mem Plotter`)
Dumps contiguous blocks of memory for array analysis, waveform verification, or histogram visualization.

| Field | Description |
| :--- | :--- |
| **Addr/Name** | Base symbol or starting memory address (e.g., `0x60001000`). |
| **Size** | Word access width: `8`, `16`, or `32` bits (`mdb`, `mdh`, `mdw`). |
| **Count** | Number of sequential memory words to dump. |
| **Style** | **Bar** (histogram representation) or **Line** (continuous waveform trace). |
| **Auto / ms** | Triggers repeated block memory reads at the selected rate. |

---

### 4. Memory File Loader (`+ Mem Loader`)
Allows batch uploading of raw hexadecimal datasets directly into target RAM. Ideal for injecting DSP filters, look-up tables (LUTs), or synthetic test vectors.

- **Select HEX File:** Accepts `.hex`, `.dat`, or `.txt` files containing space-, comma-, or newline-separated hexadecimal values (with or without `0x` prefixes).
- **Chunked Batching:** Writes are grouped into 30-word transaction chunks to prevent socket buffer exhaustion while the target core is briefly halted.

---

### 5. Alive Light Indicator (`+ Alive Light`)
Provides immediate visual validation of system health, RTOS heartbeat counters, or fault states.

- **Green (TRUE):** The target variable evaluates to non-zero ($>0$).
- **Dark Gray (FALSE):** The target variable evaluates to zero ($0$).
- **Red (ERROR):** JTAG communication failure, symbol missing, or parse error.
- Includes both a manual **Read Now** trigger and an autonomous **Auto Poll** switch.

---

## 5. OpenOCD Integration & Target Control

The GUI natively handles the initialization and hardware reset sequence without requiring an external terminal or IDE.

### Launch Mechanism
When clicking **Start OpenOCD**, the GUI invokes:

```bash
openocd -s <config_directory> -f <main_config_file>
```

- The `-s` search parameter ensures that any nested configuration calls (e.g., `source [find interface/fp6.cfg]`) resolve correctly regardless of user working paths.
- A visible console window is deliberately spawned (`CREATE_NEW_CONSOLE`), allowing real-time visibility into USB driver initialization, JTAG scan chain discovery, and hardware warning messages.

### Target Reset (`reset run`)
Clicking **Reset CPU** dispatches the OpenOCD hardware primitive:

```tcl
reset run
```

Unlike GDB-mediated resets that halt execution at the reset vector or intercept `main()`, `reset run` toggles the system reset line and releases the processor immediately, enabling standard, autonomous boot execution.

---

## 6. Getting Started & Workflow

### Prerequisites
1. **Python 3.8+** installed.
2. Required Python libraries:

   ```bash
   pip install customtkinter matplotlib
   ```

3. **OpenOCD Binary & Drivers:** Ensure OpenOCD is installed (either bundled with SoftConsole or standalone). If running without SoftConsole, install the **Microchip FlashPro Express** driver package to provide the underlying USB/JTAG device drivers (alongside the required `libusb`/`libftdi` DLLs in the OpenOCD folder).

### Typical Debug Session
1. Power up the target hardware and attach the JTAG programmer.
2. Launch the application:

   ```bash
   python RiscVLiveDebugger.py
   ```

3. If managing OpenOCD from the GUI:
   - Browse to `openocd.exe`.
   - Browse to your target `.cfg` script.
   - Click **Start OpenOCD** and verify the terminal displays `Listening on port 6666 for tcl connections`.
4. Click **Upload .map File** and select your firmware's `.map` file.
5. Add required monitors and sliders using the top control bar.
6. Configure widgets with variable names (e.g., `telemetry_counter`, `control_gain`).
7. Save your layout using **Save Layout** (`layout.json`) for instant restoration in future sessions.

---

## 7. Failure Modes, Edge Cases & Limitations

While this tool is engineered to be resilient, embedded developers should understand the underlying constraints of JTAG debug interfaces:

### 1. Hardware Watchdog Timers (WDT)
If your firmware enables a hardware watchdog timer, prolonged halts or high polling rates on large memory blocks may trigger a watchdog reset if the JTAG access duration exceeds the hardware watchdog timeout.
- *Mitigation:* Ensure polling intervals provide sufficient margin, or refresh the watchdog via an interrupt unaffected by temporary microsecond core halts.

### 2. High Polling Rates vs. JTAG Clock Speed
Setting multiple widgets to poll at `50 ms` over low-frequency JTAG adapters ($\le 1\text{ MHz}$) will saturate the debug bus, leading to socket timeouts.
- *Mitigation:* Keep the aggregate polling rate matched to your JTAG adapter's maximum throughput (typically `200–500 ms` for continuous variables).

### 3. Zombie OpenOCD Processes
If the GUI is terminated abruptly via a task manager or process kill signal while OpenOCD is running, the underlying `openocd.exe` process may remain bound to TCP port 6666.
- *Symptom:* Subsequent attempts to run OpenOCD fail with `Address already in use`.
- *Remedy:* Terminate any dangling instances using Task Manager or the command prompt:

  ```cmd
  taskkill /F /IM openocd.exe
  ```

### 4. Direct Register Access Caveat
While memory-mapped peripheral registers can be accessed by their physical addresses (e.g., `0x40000000`), reading certain peripheral registers (such as FIFO buffers or clear-on-read interrupt flag registers) via the GUI will trigger side-effects in hardware. Restrict live tuning primarily to static RAM variables and safe configuration registers.

---

## 8. Roadmap & Future Improvements

### Non-Halting Mode via System Bus Access (SBA)
In the **RISC-V External Debug Support Specification**, a Debug Module can access target memory either via **CPU-Mediated Program Buffer execution** or via a dedicated **System Bus Access (SBA)** master:

| Characteristic | CPU-Mediated Access (Current) | System Bus Access (SBA) |
| :--- | :--- | :--- |
| **Hardware Mechanism** | Injects `lw`/`sw` instructions executed by the halted RISC-V core's LSU. | Dedicated bus master inside the Debug Module connected directly to AXI4 / AHB / TileLink. |
| **Core Halt Required?** | **Yes** (`halt; wait_halt` ➔ `op` ➔ `resume`). | **No.** Reads and writes occur while the CPU runs at full speed. |
| **Real-Time Jitter** | Introduces microsecond execution pauses during JTAG transactions. | **Zero CPU stalls** (only standard interconnect bus arbitration). |
| **D-Cache Coherency** | **Coherent** with the CPU's L1 D-Cache (traverses the core's LSU). | **Non-coherent** (bypasses L1 D-Cache; sees physical RAM/bus only). |
| **Uncached Memory Rule** | Mandatory only for FPGA fabric / DMA-shared data. | **Strictly mandatory for all variables** (cached CPU writes are invisible to SBA). |
| **PolarFire Mi-V Support** | **Supported** (`MIV_RV32` / `MIV_ESS`). | **Not implemented** (omitted to conserve FPGA LUT/fabric resources). |

Because Microchip's `MIV_RV32` soft-core omits the optional SBA hardware block, the default atomic `halt` ➔ `op` ➔ `resume` sequence is required for PolarFire Mi-V targets. 

However, on third-party RISC-V cores that implement SBA—or on ARM Cortex-M targets utilizing AHB-AP/AXI-AP—OpenOCD can perform non-halting memory transfers directly over the system bus. Adding an optional **"Non-Halting (SBA) Mode"** toggle (which bypasses `halt` and `resume` commands) is planned as a future enhancement for SBA-capable targets. *Note: When using SBA mode, both read and write variables must reside in uncached memory to avoid D-Cache incoherency.*

---

## 9. License & Attribution

This project is licensed under the **Apache License, Version 2.0**. You may freely use, modify, and distribute this software for personal, academic, or commercial purposes.

### Notice
See the accompanying `NOTICE` file for copyright, author attribution, and project repository details. When creating derivative tools or distributing modified versions, proper attribution to the original author must be maintained according to the terms of the Apache 2.0 License.