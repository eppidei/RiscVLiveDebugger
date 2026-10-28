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
7. [Failure Modes, Edge Cases & Limitations](#7-failure-modes--edge-cases--limitations)
8. [License & Attribution](#8-license--attribution)

---

## 1. Motivation: The PolarFire / Mi-V Debugging Problem

Developing real-time embedded firmware on FPGA soft-cores (such as the Microchip Mi-V core implemented in PolarFire FPGA fabric) reveals significant deficiencies in traditional IDE-based toolchains (e.g., SoftConsole, Eclipse, and GDB):

* **Invasive Halts Destroy Real-Time Systems:** Traditional debugging relies on hardware software breakpoints. Halting the CPU freezes PWM generation, motor commutation, closed-loop PID controllers, and active communication buses (SPI, CAN, Ethernet), frequently triggering hardware faults or destructive over-current events.
* **GDB Polling Instability:** Continuous watch-expression polling over GDB often leads to serial/JTAG communication packet desynchronization, crashing the GDB server and necessitating complete hardware power cycles.
* **Lack of Specialized Instrumentation:** Standard debuggers cannot natively render live time-series plots, decode fixed-point ($Q$-format) fractions, parse hardware bitfield registers dynamically, or plot memory arrays as histograms.
* **Heavyweight Footprint:** Requiring a multi-gigabyte IDE installation just to tune control parameters, inspect buffers, or test hardware in a production or lab environment introduces unnecessary overhead.

### The Solution

This tool bypasses GDB entirely. It communicates with OpenOCD via a lightweight TCP/IP socket connection directly to its TCL command interface (port 6666). The core is halted, inspected or modified, and resumed within microsecond windows, providing real-time data acquisition without destabilizing control algorithms or requiring an active IDE session.

---

## 2. Architectural Overview

```
+-------------------------------------------------------------------+
|               Live Memory Tuner GUI (CustomTkinter)               |
|  [VarSlider]   [ReadMonitor]   [MemPlotter]   [AliveLight]        |
+---------------------------------+---------------------------------+
                                  |
               Local TCP Socket (Port 6666 / TCL IPC)
               * Atomic "Connect -> Halt -> Op -> Resume -> Close"
                                  |
+---------------------------------v---------------------------------+
|                       OpenOCD Server Process                      |
|                  (Embedded or Standalone Instance)                |
+---------------------------------+---------------------------------+
                                  |
                   JTAG / SWD Hardware Interface
                   (Microchip FlashPro / FTDI / J-Link)
                                  |
+---------------------------------v---------------------------------+
|                    Target System (PolarFire FPGA)                 |
|  +--------------------+             +--------------------------+  |
|  |   Mi-V RISC-V Core | <---Bus---> |      Uncached LSRAM      |  |
|  |  (Running Firmware)|             |    Mapped at 0x60000000  |  |
|  +--------------------+             +--------------------------+  |
+-------------------------------------------------------------------+
```

### The 3-Step Atomic Sequence

To avoid debug bus collisions, target panics, or race conditions with the running firmware, all transactions execute an atomic 3-step sequence:

```text
+-------------------+      +----------------------+      +----------+
|  halt; wait_halt  | ---> | read / write command | ---> |  resume  |
+-------------------+      +----------------------+      +----------+

Because OpenOCD handles this at the hardware level, the core halts only for the handful of clock cycles required to service the debug bus request, making it virtually transparent to human perception and real-time loops.

### Connection-Per-Command Strategy

Rather than maintaining a single persistent TCP socket—which can corrupt data frames when multiple asynchronous polling threads collide or when the hardware is reset—every read/write operation opens a local TCP connection, executes its command, and closes the connection. Operating across `localhost`, the connection overhead is $<0.1\text{ ms}$, ensuring complete thread safety and auto-recovery from hardware resets.

---

## 3. Firmware Requirements & Best Practices

To enable live parameter modification and telemetry acquisition without unexpected behavior, firmware running on the target must follow key conventions:

### 1. Declare Telemetry Variables as `volatile`
If a variable is not marked `volatile`, the compiler's optimization pass (`-O2`, `-O3`) will keep the value in internal processor registers (e.g., `a0`–`a7`, `s0`–`s11`) and avoid reading from physical RAM during tight loops. Changes written by the GUI over JTAG will be ignored by the CPU.

```c
/* INCORRECT: The compiler may optimize out memory re-reads */
uint32_t motor_target_rpm = 1000;

/* CORRECT: Forces the CPU to read and write to actual memory every time */
volatile uint32_t motor_target_rpm = 1000;
```

### 2. Global vs. Static Linkage (Symbol Table Resolution)
The GUI resolves variable addresses by parsing the GNU Linker `.map` file.
* Variables must have **global linkage** so their names appear cleanly in the symbol table.
* Avoid declaring monitored variables as `static` inside local function blocks, as the linker will either strip the symbol name or assign a mangled, non-unique identifier.

```c
/* In telemetry.c */
volatile float pid_kp = 1.25f;       /* Visible in .map file */
volatile uint32_t system_status = 0; /* Visible in .map file */
```

### 3. Place Monitored Data in Uncached Memory
On processors with data caches (D-Cache) enabled (such as cached Mi-V implementations):
* If the GUI writes to a cached RAM address via JTAG, the CPU may continue reading dirty data from its internal D-Cache.
* Conversely, if the CPU writes updates to the cache without an immediate write-back, the GUI will read stale data from physical RAM.

**Solution:** Place telemetry and tuning variables into an **uncached memory region**. On PolarFire Mi-V architectures, this is typically mapped to the internal Large SRAM (LSRAM) at `0x60000000`:

```c
/* In your firmware: using a linker section attribute */
__attribute__((section(".uncached_lsram"))) volatile uint32_t live_tuning_var;
```

```ld
/* In your linker script (.ld): */
MEMORY
{
    DDR_CACHED (rwx) : ORIGIN = 0x80000000, LENGTH = 256M
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

---

## 4. Widget Reference & Field Configuration

### Top Control Bar
* **Upload .map File:** Selects the GNU Linker map file produced during firmware build. Once loaded, all global variable names can be used directly across all widgets in place of hex addresses.
* **OpenOCD Exe & Main CFG:** Paths pointing to the local `openocd.exe` binary and the board initialization `.cfg` script.
* **Start / Stop OpenOCD:** Spawns a dedicated console window hosting OpenOCD with the working directory configured to find nested configuration dependencies.
* **Reset CPU:** Dispatches `reset run` over port 6666, forcing a clean hardware reboot without setting GDB breakpoints.
* **Save / Load Layout:** Exports or imports the current dashboard configuration to a `.json` file.

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

* **Fixed-Point Scaling (Frac Bits):** Converts raw integer representation of fixed-point arithmetic ($Q$-format) to real engineering units:
  $$\text{Displayed Value} = \frac{\text{Raw Integer}}{2^{\text{Frac Bits}}}$$
* **Bitfield Inspector:** Expands up to four discrete sub-fields within the word. Enter bit slices using `high:low` notation (e.g., `7:4`) or single bits (e.g., `2`). Automatically displays unsigned values and hexadecimal representations.
* **Time-Series Plotting:** Real-time matplotlib line graph with configurable sample buffer sizes (default 100), manual or automated Y-axis scaling, and a buffer reset function.
* **Auto-Polling:** Polling interval configurable down to 50 ms.

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

* **Select HEX File:** Accepts `.hex`, `.dat`, or `.txt` files containing space-, comma-, or newline-separated hexadecimal values (with or without `0x` prefixes).
* **Chunked Batching:** Writes are grouped into 30-word transaction chunks to prevent socket buffer exhaustion while the target core is briefly halted.

---

### 5. Alive Light Indicator (`+ Alive Light`)
Provides immediate visual validation of system health, RTOS heartbeat counters, or fault states.

* **Green (TRUE):** The target variable evaluates to non-zero ($>0$).
* **Dark Gray (FALSE):** The target variable evaluates to zero ($0$).
* **Red (ERROR):** JTAG communication failure, symbol missing, or parse error.
* Includes both a manual **Read Now** trigger and an autonomous **Auto Poll** switch.

---

## 5. OpenOCD Integration & Target Control

The GUI natively handles the initialization and hardware reset sequence without requiring an external terminal or IDE.

### Launch Mechanism
When clicking **Start OpenOCD**, the GUI invokes:
```bash
openocd -s <config_directory> -f <main_config_file>
```
* The `-s` search parameter ensures that any nested configuration calls (e.g., `source [find interface/fp6.cfg]`) resolve correctly regardless of user working paths.
* A visible console window is deliberately spawned (`CREATE_NEW_CONSOLE`), allowing real-time visibility into USB driver initialization, JTAG scan chain discovery, and hardware warning messages.

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
3. **OpenOCD Binary & Drivers:** Ensure OpenOCD is installed (either bundled with SoftConsole or standalone). If running without SoftConsole, install the **Microchip FlashPro Express** driver package to provide the underlying USB/JTAG device drivers.

### Typical Debug Session
1. Power up the target hardware and attach the JTAG programmer.
2. Launch the application:
   ```bash
   python polarfire_gui.py
   ```
3. If managing OpenOCD from the GUI:
   * Browse to `openocd.exe`.
   * Browse to your target `.cfg` script.
   * Click **Start OpenOCD** and verify the terminal displays `Listening on port 6666 for tcl connections`.
4. Click **Upload .map File** and select your firmware's `.map` file.
5. Add required monitors and sliders using the top control bar.
6. Configure widgets with variable names (e.g., `telemetry_counter`, `control_gain`).
7. Save your layout using **Save Layout** (`layout.json`) for instant restoration in future sessions.

---

## 7. Failure Modes, Edge Cases & Limitations

While this tool is engineered to be resilient, embedded developers should understand the underlying constraints of JTAG debug interfaces:

### 1. Hardware Watchdog Timers (WDT)
If your firmware enables a hardware watchdog timer, prolonged halts or high polling rates on large memory blocks may trigger a watchdog reset if the JTAG access duration exceeds the hardware watchdog timeout.
* *Mitigation:* Ensure polling intervals provide sufficient margin, or refresh the watchdog via an interrupt unaffected by temporary microsecond core halts.

### 2. High Polling Rates vs. JTAG Clock Speed
Setting multiple widgets to poll at $50\text{ ms}$ over low-frequency JTAG adapters ($\le 1\text{ MHz}$) will saturate the debug bus, leading to socket timeouts.
* *Mitigation:* Keep the aggregate polling rate matched to your JTAG adapter's maximum throughput (typically $200\text{--}500\text{ ms}$ for continuous variables).

### 3. Zombie OpenOCD Processes
If the GUI is terminated abruptly via a task manager or process kill signal while OpenOCD is running, the underlying `openocd.exe` process may remain bound to TCP port 6666.
* *Symptom:* Subsequent attempts to run OpenOCD fail with `Address already in use`.
* *Remedy:* Terminate any dangling instances using Task Manager or the command prompt:
  ```cmd
  taskkill /F /IM openocd.exe
  ```

### 4. Direct Register Access Caveat
While memory-mapped peripheral registers can be accessed by their physical addresses (e.g., `0x40000000`), reading certain peripheral registers (such as FIFO buffers or clear-on-read interrupt flag registers) via the GUI will trigger side-effects in hardware. Restrict live tuning primarily to static RAM variables and safe configuration registers.

---

## 8. License & Attribution

This project is licensed under the **Apache License, Version 2.0**. You may freely use, modify, and distribute this software for personal, academic, or commercial purposes.

### Notice
See the accompanying `NOTICE` file for copyright, author attribution, and project repository details. When creating derivative tools or distributing modified versions, proper attribution to the original author must be maintained according to the terms of the Apache 2.0 License.