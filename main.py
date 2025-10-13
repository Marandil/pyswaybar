#!/usr/bin/env -S python3 -u

import asyncio
import json
import time
import subprocess
import sys
import psutil

from dataclasses import dataclass
from collections import defaultdict
from typing import Any, Callable

font_monospace = 'RobotoMonoNerdFont'

nf_md_volume_off = '\U000f0581'
nf_md_volume_low = '\U000f057f'
nf_md_volume_medium = '\U000f0580'
nf_md_volume_high = '\U000f057e'

nf_md_arrow_up_thick = '\U000f005e'
nf_md_arrow_down_thick = '\U000f0046'

nf_fa_globe = '\uf0ac'
nf_fa_desktop = '\uf108'
nf_fa_network_wired = '\uef09'
nf_fa_wifi = '\uf1eb'

def autoconvert(x: int, binary=True):
    if binary:
        if x < 1024: 
            return str(x)
        x //= 1024
        if x < 1024:
            return f"{x}Ki"
        x //= 1024
        if x < 1024:
            return f"{x}Mi"
        x //= 1024
        if x < 1024:
            return f"{x}Gi"
        x //= 1024
        return f"{x}Ti"
    else:
        raise NotImplementedError()


async def create_file_reader(file, limit=65536):
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader(limit=limit, loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    await loop.connect_read_pipe(lambda: protocol, file)
    return reader


class JSONStreamInput:
    def __init__(self,
                 reader: asyncio.StreamReader,
                 callback: Callable[[Any], None],
                 buffer_limit: int = 65536,
                 decode_grace: int = 100,
                 array_format: bool = False):
        self.reader = reader
        self.callback = callback
        self.buffer = ''
        self.buffer_limit = buffer_limit
        self.json_decoder = json.JSONDecoder()
        self.default_grace = decode_grace
        self.decode_grace: int  # set to default in process_buffer
        self.array_format = array_format

    def process_buffer(self):
        self.decode_grace = self.default_grace
        try:
            while self.buffer:
                while self.array_format and self.buffer.startswith(','):
                    self.buffer = self.buffer[1:].lstrip()
                payload, end = self.json_decoder.raw_decode(self.buffer)
                self.decode_grace = self.default_grace
                self.buffer = self.buffer[end:].lstrip()
                while self.array_format and self.buffer.startswith(','):
                    self.buffer = self.buffer[1:].lstrip()
                self.callback(payload)
        except json.JSONDecodeError:
            # print(f"{self.__class__} {e=} {self.buffer=} {self.decode_grace=}", file=sys.stderr)
            self.decode_grace -= 1
            self.buffer = self.buffer.lstrip()  # lstrip should always be safe
            if not self.decode_grace:
                raise

    async def read_forever(self):
        if self.array_format:
            curr_buffer = await self.reader.read(1)
            assert curr_buffer == b'['
        while not self.reader.at_eof():
            curr_buffer = await self.reader.read(self.buffer_limit)
            self.buffer += curr_buffer.decode()
            self.process_buffer()


class PipeWireMonitor:
    def __init__(self):
        self.task = asyncio.create_task(self.read_forever())
        self.nodes = {}
        self.nodes_by_name = {}
        self.devices = {}
        self.metadata: dict[str, dict[str, Any]] = defaultdict(dict)
        # ---
        self.volume = 0
        self.is_muted = False
        self.event = asyncio.Event()

    def notify_change(self):
        self.event.set()

    def try_delete_node(self, id: int):
        node = self.nodes.pop(id, None)
        if node is not None:
            del self.nodes_by_name[node['props']['node.name']]
            return True
        else:
            return False

    def try_delete_device(self, id: int):
        device = self.devices.pop(id, None)
        return device is not None

    def process_update(self, update: list[dict[str, Any]]):
        check_volume_update = False
        for entry in update:
            match entry:
                case {'id': id, 'info': None}:
                    self.try_delete_node(id) or self.try_delete_device(id)

                case {'id': id, 'type': "PipeWire:Interface:Metadata", 'props': props, 'metadata': metadata}:
                    self.metadata[props['metadata.name']].update(
                        {m['key']: m['value'] for m in metadata}
                    )
                    check_volume_update = True

                case {'id': id, 'type': "PipeWire:Interface:Node", 'info': info}:
                    node_name = info['props']['node.name']
                    self.nodes[id] = info
                    self.nodes_by_name[node_name] = info
                    check_volume_update = True

                case {'id': id, 'type': "PipeWire:Interface:Device", 'info': info}:
                    self.devices[id] = info
                    check_volume_update = True

                case _:
                    pass

        if check_volume_update:
            try:
                default_audio_sink = self.metadata['default']['default.audio.sink']['name']
                node = self.nodes_by_name[default_audio_sink]
                props = [p for p in node['params']['Props'] if 'volume' in p]
                if not props:
                    print(f"ERROR: missing volume props for node {default_audio_sink}", file=sys.stderr)
                volume = props[0]['volume']
                channelVolumes = props[0]['channelVolumes']
                volume *= sum(channelVolumes) / len(channelVolumes)
                volume = round(pow(volume, 1/3), 2)
                is_muted = props[0]['mute']
                if self.volume != volume or self.is_muted != is_muted:
                    self.volume = volume
                    self.is_muted = is_muted
                    self.notify_change()

            except KeyError:
                pass

    async def read_forever(self):
        self.proc = await asyncio.create_subprocess_exec("pw-dump", "-m", stdout=subprocess.PIPE)
        stdout = self.proc.stdout
        json_stream_input = JSONStreamInput(stdout, self.process_update)
        await json_stream_input.read_forever()

    async def close(self):
        self.proc.terminate()
        await self.task


class NetworkMonitor:
    @dataclass
    class IfStats:
        is_up: bool = False
        bytes_sent: int = 0
        bytes_recv: int = 0
        packets_sent: int = 0
        packets_recv: int = 0
        last_update: int = 0
        bytes_sent_rate: int = 0
        bytes_recv_rate: int = 0
        packets_sent_rate: int = 0
        packets_recv_rate: int = 0

    interfaces: dict[str, IfStats]

    def __init__(self):
        IfStats = self.IfStats
        self.interfaces = defaultdict(lambda: IfStats())

    def update(self):
        net_stats = psutil.net_if_stats()
        net_ioctr = psutil.net_io_counters(pernic=True, nowrap=True)
        for if_name, if_stats in net_stats.items():
            ioctrs = net_ioctr[if_name]
            iface = self.interfaces[if_name]
            iface.is_up = if_stats.isup
            last_update = time.perf_counter()
            if iface.last_update:
                bytes_sent_delta = ioctrs.bytes_sent - iface.bytes_sent
                bytes_recv_delta = ioctrs.bytes_recv - iface.bytes_recv
                packets_sent_delta = ioctrs.packets_sent - iface.packets_sent
                packets_recv_delta = ioctrs.packets_recv - iface.packets_recv
                last_update_delta = last_update - iface.last_update
                iface.bytes_sent_rate = bytes_sent_delta / last_update_delta
                iface.bytes_recv_rate = bytes_recv_delta / last_update_delta
                iface.packets_sent_rate = packets_sent_delta / last_update_delta
                iface.packets_recv_rate = packets_recv_delta / last_update_delta
            iface.bytes_sent = ioctrs.bytes_sent
            iface.bytes_recv = ioctrs.bytes_recv
            iface.packets_sent = ioctrs.packets_sent
            iface.packets_recv = ioctrs.packets_recv
            iface.last_update = last_update


def fmt_stacked(text1: str, text2: str):
    """
    Highly experimental, not really reliable
    """
    t1l = len(text1)
    t2l = len(text2)
    fs = 7*1024
    rise = 4096
    sp = int(1.6 * fs)
    padding = "" if t2l > t1l else '\u00a0'*(t1l-t2l)
    return f'<span font="{font_monospace}"><span rise="{rise*3//2}" font_size="{fs}">{text1}</span><span letter_spacing="-{sp}" font_size="{fs}">{"\u00a0"*t1l}</span><span rise="-{rise//2}" font_size="{fs}">{text2}{padding}</span></span>'

def fmt_network(nm: NetworkMonitor):
    all_ifaces = []
    for if_name, iface in nm.interfaces.items():
        if iface.is_up:
            recv = autoconvert(iface.bytes_recv)
            sent = autoconvert(iface.bytes_sent)
            if if_name == 'lo':
                if_icon = nf_fa_desktop
            elif if_name.startswith('wlan'):
                sfx = if_name.removeprefix('wlan')
                if_icon = f"{nf_fa_wifi} <sub>{sfx}</sub>"
            elif if_name.startswith('tailscale'):
                sfx = if_name.removeprefix('tailscale')
                if_icon = f"{nf_fa_globe} <sub>{sfx}</sub>"
            elif if_name.startswith('en'):
                sfx = if_name.removeprefix('en')
                if_icon = f"{nf_fa_network_wired} <sub>{sfx}</sub>"
            else:
                if_icon = if_name
            fmt_send = f"{nf_md_arrow_up_thick} {sent}B"
            fmt_recv = f"{nf_md_arrow_down_thick} {recv}B"
            all_ifaces.append(f"[ {if_icon} {fmt_stacked(fmt_send, fmt_recv)} ]")
    return " ".join(all_ifaces)


def fmt_volume(pwm: PipeWireMonitor):
    # # Get sink volume from wpctl
    # sink_volume_call = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], capture_output=True)
    # volume = sink_volume_call.stdout.decode().strip().removeprefix("Volume: ")
    # is_muted = volume.endswith("[MUTED]")
    # volume = volume.removesuffix("[MUTED]")
    # volume = float(volume)
    volume, is_muted = pwm.volume, pwm.is_muted
    if is_muted:
        icon = nf_md_volume_off
    elif volume < 0.3:
        icon = nf_md_volume_low
    elif volume < 0.6:
        icon = nf_md_volume_medium
    else:
        icon = nf_md_volume_high

    return f"{icon} {volume:.0%}"


def fmt_time():
    fmt_now = time.strftime("%Y-%m-%d %H:%M:%S")
    return fmt_now


async def sub_call(*args):
    proc = await asyncio.create_subprocess_exec(*args)
    await proc.communicate()


def act_toggle_mute():
    asyncio.create_task(sub_call("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"))


async def main():
    use_protocol = True
    pwm = PipeWireMonitor()
    nm = NetworkMonitor()

    fld_time = fld_volume = fld_network = fld_event = "(startup)"
    update_queue = asyncio.Queue()

    async def update_time_task():
        nonlocal fld_time
        N = 599
        while True:
            # First time each N seconds sleep less than 1 second to align with the wall clock
            fld_time = fmt_time()
            await update_queue.put(None)
            adj = time.clock_gettime(time.CLOCK_REALTIME) % 1
            await asyncio.sleep(1 - adj)
            for _ in range(N):
                fld_time = fmt_time()
                await update_queue.put(None)
                await asyncio.sleep(1)

    async def update_volume_task():
        nonlocal fld_volume
        while True:
            fld_volume = fmt_volume(pwm)
            await update_queue.put(None)
            await pwm.event.wait()
            pwm.event.clear()

    async def update_network_task():
        nonlocal fld_network
        while True:
            nm.update()
            fld_network = fmt_network(nm)
            await asyncio.sleep(5)

    async def process_stdin():
        def update(event):
            nonlocal fld_event
            # fld_event = f'{event=}'
            name, rel_x, rel_y, width, height = \
                event['name'], event['relative_x'], event['relative_y'], event['width'], event['height']
            x_pct = rel_x / width
            y_pct = rel_y / height
            fld_event = f'{name} @ {x_pct:3.1%}, {y_pct:3.1%}'
            match name:
                case 'volume' if x_pct < 0.5:
                    act_toggle_mute()
                case _:
                    fld_event += " [unhandled]"
            asyncio.create_task(update_queue.put(None))

        reader = await create_file_reader(sys.stdin)
        json_stream_input = JSONStreamInput(reader, update, array_format=True)
        await json_stream_input.read_forever()


    updaters = [
        asyncio.create_task(update_time_task()),
        asyncio.create_task(update_volume_task()),
        asyncio.create_task(update_network_task()),
    ]
    if use_protocol:
        updaters.append(asyncio.create_task(process_stdin()))

    try:
        if use_protocol:
            print('{"version":1,"click_events":true}\n[')
            while True:
                try:
                    msg = [
                        # Uncomment event to debug mouse events
                        # {"full_text":fld_event, "name":"event"},
                        {"full_text":fld_network, "name":"network", "markup":"pango"},
                        {"full_text":fld_volume, "name":"volume"},
                        {"full_text":fld_time, "name":"clock"},
                    ]
                    msg = json.dumps(msg)
                    print(f'{msg},')
                    await update_queue.get()
                except Exception as e:
                    print(f"Error: {e!r}")
        else:
            while True:
                try:
                    print(f"{fld_network} {fld_volume} {fld_time}")
                    await update_queue.get()
                except Exception as e:
                    print(f"Error: {e!r}")
    finally:
        for task in updaters:
            task.cancel()
        await pwm.close()

asyncio.run(main())

