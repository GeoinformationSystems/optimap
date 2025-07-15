ARG UBUNTU_VERSION=22.04

FROM ubuntu:${UBUNTU_VERSION}

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

ENV OPTIMAP_DEBUG=False
ENV OPTIMAP_ALLOWED_HOST=*

ENV DEBIAN_FRONTEND="noninteractive" TZ="Europe/Berlin"

# install Python
RUN apt-get update && \
    apt-get install -y -qq python-is-python3 && \
    apt-get install -y -qq python3-pip tzdata

# install GDAL from UbuntuGIS
RUN apt-get update && \
    apt-get install -y -qq software-properties-common && \
    add-apt-repository ppa:ubuntugis/ppa && \
    apt-get install -y -qq gdal-bin libgdal-dev

RUN pip install gdal=="$(gdal-config --version).*"

RUN mkdir -p /code

WORKDIR /code

COPY requirements.txt /tmp/requirements.txt

RUN set -ex && \
    pip install --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt && \
    rm -rf /root/.cache/

COPY . /code

RUN chmod a+x /code/etc/manage-and-run.sh

EXPOSE 8000

#CMD ["gunicorn", "--bind", ":8000", "--workers", "2", "optimap.wsgi"]
#CMD ["python", "manage.py", "runserver", "0.0.0.0:8000" ]
ENTRYPOINT [ "/code/etc/manage-and-run.sh" ]
