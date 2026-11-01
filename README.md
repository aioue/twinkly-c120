# C.120 for Twinkly

![Preview: 480 frames at 4 fps, one revolution in 120 seconds](preview.gif)



C.120 is a rainbow movie for a Twinkly RGBW string. Two rainbows run along the LEDs. The file is 2160 frames at 18 fps, so one pass through the colour wheel takes two minutes if the string plays that frame rate. The white channel is left off. After the upload, the controller plays the movie with this computer off.

An example gif of the pattern in action is above ⬆️

`c120.py` talks to the string on your LAN. It uploads only when you pass `--yes`, and only when movie storage is empty. It does not delete a movie, update firmware, or change Wi-Fi. `plan` does not use the network. `inspect` and `upload` log in, which ends any other app's session on the string. The Twinkly app has to connect again.

You need Python 3.9 or newer. There is nothing to install. On firmware 2.9 and later, Compatibility Mode has to be on. The script refuses a string that is not RGBW, a string that cannot store 2160 frames, and a string that already has a movie.

```bash
python3 c120.py plan
python3 c120.py inspect 192.168.1.50
python3 c120.py upload 192.168.1.50
python3 c120.py upload 192.168.1.50 --yes
```

Use the string's address from the Twinkly app or from your router's client list. If inspect times out, this computer cannot reach that address.

The dimmer is set to 80%. The colour bytes stay at full range. Generation II keeps the movie across unplug and loses the dimmer, so the string can come back at full brightness. Set 80% again in the app if you want it. See [what happens if you unplug the device](https://help.twinkly.com/hc/en-gb/articles/18277762892189-what-happens-if-i-unplug-my-twinkly-device-from-the-power-source). Compatibility Mode is the switch that leaves the local API on: [Twinkly's explanation](https://help.twinkly.com/hc/en-gb/articles/30221117179165-What-is-Compatibility-Mode).

On a Twinkly Strings 250 RGBW (product code TWS250SPP2, firmware 2.9.1) the controller stored this movie as 17 fps and kept the 56 ms frame delay. The loop was not timed with a clock. After that string was unplugged and plugged back in, the movie started again and looked smooth.

If an upload errors after it has started writing, look in the app before trying again. A half-written movie is left where it is.

The local API has no password. Anyone who can reach the string on the network can upload a movie.

`python3 -m unittest test_c120.py` checks the encoder and a fake controller. It does not talk to a real string.

MIT license.
