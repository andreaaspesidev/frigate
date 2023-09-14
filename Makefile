version:
	echo "VERSION='1.0.0-andreaaspesi'" > frigate/version.py

web:
	docker build --tag frigate-web --file docker/Dockerfile.web web/

wheels:
	docker build --tag andreaaspesi/frigate-wheels:1.0.0 --file docker/Dockerfile.wheels .

ffmpeg:
	docker build --tag andreaaspesi/frigate-ffmpeg:1.0.0 --file docker/Dockerfile.ffmpeg .

frigate: version
	docker build --build-arg FFMPEG_VERSION=1.0.0 --build-arg WHEELS_VERSION=1.0.0 --tag andreaaspesi/frigate:2.0.0 --file docker/Dockerfile.frigate .

push:
	docker push andreaaspesi/frigate-wheels:1.0.0
	docker push andreaaspesi/frigate-ffmpeg:1.0.0
	docker push andreaaspesi/frigate:2.0.0

all: web wheels ffmpeg frigate

default_target: frigate

.PHONY: web
