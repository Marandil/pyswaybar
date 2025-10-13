# pyswaybar

A quick-and-dirty Swaybar server in Python.
Assumes PipeWire audio.

## Setup

```bash
$ cp ./main.py ~/.config/sway/sway-bar-status

$ $EDITOR ~/.config/sway/config
```

```conf
# Read `man 5 sway-bar`
bar {
    position top

    status_command ~/.config/sway/sway-bar-status

    colors {
        // Up to you
    }
}
```

## Configuration

For now just edit the source. No explicit configuration options available.

