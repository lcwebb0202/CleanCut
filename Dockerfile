FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility,video \
    HF_HOME=/models/hf

# Jellyfin's ffmpeg build: modern (Ubuntu's 4.4 shifts AAC audio ~50-100 ms in MKV), with NVENC.
ARG JF_FFMPEG=https://repo.jellyfin.org/files/ffmpeg/ubuntu/7.x/7.1.4-3/amd64/jellyfin-ffmpeg7_7.1.4-3-jammy_amd64.deb
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip curl ca-certificates \
 && curl -fL -o /tmp/jf.deb "$JF_FFMPEG" \
 && echo "4fccc21952a0badcb50261c3120eaaa57d726cee97339c6426bb5bcfc4dec3a5  /tmp/jf.deb" | sha256sum -c - \
 && apt-get install -y --no-install-recommends /tmp/jf.deb \
 && ln -s /usr/lib/jellyfin-ffmpeg/ffmpeg /usr/local/bin/ffmpeg \
 && ln -s /usr/lib/jellyfin-ffmpeg/ffprobe /usr/local/bin/ffprobe \
 && rm -rf /tmp/jf.deb /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
# faster-whisper pulls in the CPU-only onnxruntime, which hides the GPU build; swap it back.
RUN pip3 install --no-cache-dir -r /app/requirements.txt \
 && pip3 install --no-cache-dir --no-deps nudenet==3.4.2 \
 && pip3 uninstall -y onnxruntime \
 && pip3 install --no-cache-dir --force-reinstall --no-deps onnxruntime-gpu==1.20.2

# NudeNet's larger, more accurate detector (the package ships a smaller one as fallback).
# Plain release links now redirect to a GitHub login page, so fetch it through the API.
RUN mkdir -p /opt/models \
 && curl -fL -H "Accept: application/octet-stream" -o /opt/models/640m.onnx \
      https://api.github.com/repos/notAI-tech/NudeNet/releases/assets/176832019 \
 && echo "04fe3d77980780c1f8297dc6d7f942fd5b3abe6942a188f742a85241e4f634eb  /opt/models/640m.onnx" | sha256sum -c -

# CLIP ViT-L/14 (ONNX) for scene-level checks: sex scenes, kissing, strippers.
ARG CLIP=https://huggingface.co/Xenova/clip-vit-large-patch14/resolve/c307790166907339eed5a9a53a249af534102536
RUN mkdir -p /opt/models/clip && cd /opt/models/clip \
 && curl -fL -o vision_model.onnx "$CLIP/onnx/vision_model.onnx" \
 && curl -fL -o text_model.onnx "$CLIP/onnx/text_model.onnx" \
 && curl -fL -o tokenizer.json "$CLIP/tokenizer.json" \
 && printf '%s\n' \
      "ff49f8aa57c7abfd26e382eb083e4dbf988505223a9bd3767dbfd4e729206709  vision_model.onnx" \
      "a86051a90491b97e2ea1d0351ef664926735bca7536034384f901915ee91fd69  text_model.onnx" \
      "72ed5c96db5729294468543e4bc75fce14ca63f58e37300290189ba1c1e52b85  tokenizer.json" \
    | sha256sum -c -

COPY entrypoint.sh /app/entrypoint.sh
COPY cleancut /app/cleancut
RUN chmod 755 /app/entrypoint.sh
WORKDIR /app
ENTRYPOINT ["/app/entrypoint.sh"]
