FROM alpine:3.8

ENV \
    LC_ALL=en_US.UTF-8 \
    LANG=en_US.UTF-8 \
    LANGUAGE=en_US.UTF-8

RUN \
    apk add --no-cache \
        openssl=1.0.2p-r0 \
        python3=3.6.6-r0 \
        tini=0.18.0-r0 && \
    python3 -m ensurepip && \
    pip3 install pip==18.01 && \
    pip3 install \
        aioftp==0.12.0

COPY server.py /server.py
COPY entrypoint.sh /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "server.py"]

RUN adduser -S ftps
USER ftps
