ARG NODE_VERSION=14.0

# Compile web
FROM node:${NODE_VERSION} as web
WORKDIR /opt/frigate
COPY web .
RUN npm install && npm run build

# Compile base
FROM andreaaspesi/raspbian-buster AS frigate

ENV FLASK_ENV=development

RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install --no-install-recommends -y \
    ffmpeg gnupg wget unzip tzdata nginx libnginx-mod-rtmp \
    && apt-get install --no-install-recommends -y \
        wget \
        # opencv dependencies
        build-essential cmake git pkg-config libgtk-3-dev \
        libavcodec-dev libavformat-dev libswscale-dev libv4l-dev \
        libxvidcore-dev libx264-dev libjpeg-dev libpng-dev libtiff-dev \
        gfortran openexr libatlas-base-dev libssl-dev\
        libtbb2 libtbb-dev libdc1394-22-dev libopenexr-dev \
        libgstreamer-plugins-base1.0-dev libgstreamer1.0-dev \
        # scipy dependencies
        gcc gfortran libopenblas-dev liblapack-dev cython

RUN pip3 -v install \
        scikit-build \
        opencv-python-headless \
        numpy \
        imutils \
        scipy \
        psutil \
        Flask \
        paho-mqtt \
        PyYAML \
        matplotlib \
        click \
        setproctitle \
        peewee \
        gevent

RUN APT_KEY_DONT_WARN_ON_DANGEROUS_USAGE=DontWarn apt-key adv --fetch-keys https://packages.cloud.google.com/apt/doc/apt-key.gpg \
    && echo "deb https://packages.cloud.google.com/apt coral-edgetpu-stable main" | sudo tee /etc/apt/sources.list.d/coral-edgetpu.list \
    && echo "libedgetpu1-max libedgetpu/accepted-eula select true" | debconf-set-selections \
    && apt-get -qq update && apt-get -qq install --no-install-recommends -y \
        libedgetpu1-max=15.0 \
    && rm -rf /var/lib/apt/lists/* /wheels \
    && (apt-get autoremove -y; apt-get autoclean -y)

RUN pip3 install \
    peewee_migrate \
    zeroconf \
    voluptuous\
    Flask-Sockets \
    gevent \
    gevent-websocket

RUN apt-get -qq update && apt-get -qq install --no-install-recommends -y \
        # ffmpeg runtime dependencies
        libgomp1 \
        # runtime dependencies
        libopenexr23 \
        libgstreamer1.0-0 \
        libgstreamer-plugins-base1.0-0 \
        libopenblas-base \
        libturbojpeg0-dev \
        libpng16-16 \
        libtiff5 \
        libdc1394-22 \
        libaom0 \
        libx265-165

## Tensorflow lite
COPY wheels/tflite_runtime-2.6.0-cp39-cp39-linux_armv7l.whl /tmp/tflite_runtime-2.6.0-cp39-cp39-linux_armv7l.whl
RUN pip3 install /tmp/tflite_runtime-2.6.0-cp39-cp39-linux_armv7l.whl \
    && rm -rf /var/lib/apt/lists/* \
    && (apt-get autoremove -y; apt-get autoclean -y)

COPY nginx/nginx.conf /etc/nginx/nginx.conf

# get model and labels
COPY labelmap.txt /labelmap.txt
RUN wget -q https://github.com/google-coral/test_data/raw/master/ssdlite_mobiledet_coco_qat_postprocess_edgetpu.tflite -O /edgetpu_model.tflite
RUN wget -q https://github.com/google-coral/test_data/raw/master/ssdlite_mobiledet_coco_qat_postprocess.tflite -O /cpu_model.tflite

WORKDIR /opt/frigate/
ADD frigate frigate/
ADD migrations migrations/

COPY --from=web /opt/frigate/build web/

COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 5000
EXPOSE 1935

CMD ["/run.sh"]