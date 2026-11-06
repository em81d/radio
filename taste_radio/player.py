"""Plays a radio stream through VLC.

Run `python -m taste_radio.player <stream url>` to play one stream (Ctrl+C to stop).
"""
import time

import vlc


class Player:
    def __init__(self, volume=80):
        # --no-video: audio only; --network-caching: ms of buffer before playback starts
        self._vlc = vlc.Instance("--no-video", "--quiet", "--network-caching=1500")
        self._mp = self._vlc.media_player_new()
        self._mp.audio_set_volume(volume)
        self.url = None

    def play(self, url):
        """Start (or switch to) a stream. Returns immediately; VLC buffers in the background."""
        if url == self.url and self.is_playing():
            return
        self._mp.stop()
        media = self._vlc.media_new(url)
        self._mp.set_media(media)
        self._mp.play()
        self.url = url

    def stop(self):
        self._mp.stop()
        self.url = None

    def volume(self, n=None):
        if n is not None:
            self._mp.audio_set_volume(max(0, min(100, int(n))))
        return self._mp.audio_get_volume()

    def state(self):
        return str(self._mp.get_state()).split(".")[-1]   # Opening, Buffering, Playing, Error...

    def is_playing(self):
        return self._mp.get_state() in (vlc.State.Opening, vlc.State.Buffering,
                                        vlc.State.Playing)

    def failed(self):
        return self._mp.get_state() in (vlc.State.Error, vlc.State.Ended)


if __name__ == "__main__":
    import sys

    url = sys.argv[1] if len(sys.argv) > 1 else "http://stream.radioparadise.com/mp3-192"
    p = Player()
    p.play(url)
    print(f"Playing {url}  (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(2)
            print(f"  state: {p.state()}", end="\r")
            if p.failed():
                print("\nStream failed.")
                break
    except KeyboardInterrupt:
        pass
    p.stop()
