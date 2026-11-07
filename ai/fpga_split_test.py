#!/usr/bin/env python3
"""Diagnostic only: send 1801 AXI-stream words as 1024 + 777 DMA packets.

Run beside matching cnn.bit/cnn.hwh:
    sudo -E python3 fpga_split_test.py --timeout 5

The HLS read_input must ignore incoming TLAST and read a fixed 1801 words.
No bitstream, HWH, model, or repository files are modified.
"""
import argparse
import os
import time
import numpy as np
from pynq import Overlay, allocate

HERE = os.path.dirname(os.path.abspath(__file__))
DDR_LIMIT = 0x40000000  # conservative: previously proven low-DDR address region
MM2S_SR = 0x04
S2MM_SR = 0x34
ERROR_MASK = 0x70


def describe_status(value):
    names = [(0x01, 'halted'), (0x02, 'idle'), (0x10, 'internal_error'),
             (0x20, 'slave_error'), (0x40, 'decode_error'),
             (0x1000, 'ioc_irq'), (0x4000, 'error_irq')]
    return f'0x{value:08x} (' + ', '.join(n for mask, n in names if value & mask) + ')'


def check_buffer(label, buf):
    start = int(buf.physical_address)
    end = start + buf.nbytes - 1
    good = start < DDR_LIMIT and end < DDR_LIMIT and start % 4 == 0
    print(f'{label}: {buf.nbytes} bytes, physical=0x{start:08x}, end=0x{end:08x}, LOW_DDR={good}', flush=True)
    if not good:
        raise RuntimeError(f'{label} not in aligned low DDR; refusing DMA transfer')


def wait_send(dma, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ms = dma.mmio.read(MM2S_SR)
        ss = dma.mmio.read(S2MM_SR)
        if (ms | ss) & ERROR_MASK:
            raise RuntimeError(f'DMA error: MM2S={describe_status(ms)} S2MM={describe_status(ss)}')
        if ms & 0x02:
            return
        time.sleep(0.002)
    raise TimeoutError(f'MM2S timeout: {describe_status(dma.mmio.read(MM2S_SR))}; '
                       f'S2MM={describe_status(dma.mmio.read(S2MM_SR))}')


def wait_recv(dma, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ms = dma.mmio.read(MM2S_SR)
        ss = dma.mmio.read(S2MM_SR)
        if (ms | ss) & ERROR_MASK:
            raise RuntimeError(f'DMA error: MM2S={describe_status(ms)} S2MM={describe_status(ss)}')
        if ss & 0x02:
            return
        time.sleep(0.002)
    raise TimeoutError(f'S2MM timeout: MM2S={describe_status(dma.mmio.read(MM2S_SR))}; '
                       f'S2MM={describe_status(dma.mmio.read(S2MM_SR))}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bit', default=os.path.join(HERE, 'cnn.bit'))
    parser.add_argument('--timeout', type=float, default=5.0)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')

    print('Loading FPGA overlay...', flush=True)
    overlay = Overlay(args.bit, download=True)
    dma = overlay.axi_dma_0
    first = second = output = None
    try:
        first = allocate(shape=(1024,), dtype=np.uint32)
        second = allocate(shape=(777,), dtype=np.uint32)
        output = allocate(shape=(7,), dtype=np.float32)
        check_buffer('INPUT 1', first)
        check_buffer('INPUT 2', second)
        check_buffer('OUTPUT', output)

        # One model-ID word (zero), then 1800 zero-valued float32 sensor readings.
        # This checks transport only; output classification is not meaningful.
        first.fill(0)
        second.fill(0)
        output.fill(np.nan)
        first.flush()
        second.flush()

        print('Starting S2MM receive (7 float32 logits)...', flush=True)
        dma.recvchannel.transfer(output)
        print('Sending packet 1: 1024 words...', flush=True)
        dma.sendchannel.transfer(first)
        wait_send(dma, args.timeout)
        print('Packet 1 completed. Sending packet 2: 777 words...', flush=True)
        dma.sendchannel.transfer(second)
        wait_send(dma, args.timeout)
        print('Packet 2 completed. Waiting for CNN response...', flush=True)
        wait_recv(dma, args.timeout)
        output.invalidate()
        print('SUCCESS: Both MM2S transfers and S2MM completed.', flush=True)
        print('Logits:', output.tolist(), flush=True)
    except Exception as exc:
        print('FAILED:', type(exc).__name__, str(exc), flush=True)
        raise SystemExit(1)
    finally:
        print('Final DMA status:', flush=True)
        print('  MM2S:', describe_status(dma.mmio.read(MM2S_SR)), flush=True)
        print('  S2MM:', describe_status(dma.mmio.read(S2MM_SR)), flush=True)
        for buf in (first, second, output):
            if buf is not None:
                buf.freebuffer()


if __name__ == '__main__':
    main()
