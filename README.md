<p align="center">
  <img align="center" alt="logo" src="docs/static/img/frigate.png">
</p>

# Frigate - NVR With Realtime Object Detection for IP Cameras

Auxiliary repository for experimental features.  
No support will be granted on this repository, refer to the principal one.

### Current Purpose
The target hardware is Raspberri Pi 4. Currently the work-in-progress features are:  
- [x] Autoconvert clips using custom ffmpeg args. This permit to use h265 cameras, even on hardware that have not the capabilities to perform an online transcoding.
- [ ] MQTT settings for Motion Detection, with online effects