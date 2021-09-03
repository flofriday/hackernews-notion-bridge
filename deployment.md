# Deployment

This is how I deploy this script, which may or may not work for you.

## Hardware & Software

I had an Raspberry Pi 4 (4GB) laying around and so that is what I used. I use
default Raspberry Pi OS lite.

## First Deploy

1. Install Python3.9, this is really annoyingly difficult and time consuming,
   as you need to compiele it yourself. I followed [this Guide](https://itheo.tech/install-python-3-9-on-raspberry-pi)
2. Clone this repository with and enter it with:

```bash
git clone https://github.com/flofriday/hackernews-notion-bridge
cd hackernews-notion-bridge
```

3. Install the dependencies with

```bash
python3.9 -m pip install -r requirements.txt
```

4. Create a systemd service file with `sudo vi /etc/systemd/system/hackernews-notion-bridge.service`

```
[Unit]
Description=hackernews-notion-bridge
After=network.target

[Service]
WorkingDirectory=/home/pi/hackernews-notion-bridge
Type=simple
User=pi
ExecStart=/usr/local/bin/python3.9 /home/pi/hackernews-notion-bridge/main.py --loop --number 30
Restart=always

[Install]
WantedBy=multi-user.target
```

5. Start the service

```bash
sudo systemctl enable hackernews-notion-bridge
sudo systemctl start hackernews-notion-bridge

# And to verify
sudo systemctl status hackernews-notion-bridge
```

## Updating deployment

```bash
cd hackernews-notion-bridge
git pull
sudo systemctl restart hackernews-notion-bridge

# And to verify
sudo systemctl status hackernews-notion-bridge
```
