ARG UBUNTU_VERSION=20.04

FROM ubuntu:${UBUNTU_VERSION}

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

ENV OPTIMAP_DEBUG=False
ENV OPTIMAP_ALLOWED_HOST=*

ENV DEBIAN_FRONTEND="noninteractive" TZ="Europe/Berlin"

# TODO: see https://www.joseferben.com/posts/django-on-flyio/ for more improvements

# install Python
RUN apt-get update && \
    apt-get install -y -qq python-is-python3 && \
    apt-get install -y -qq python3-pip

# install GDAL from UbuntuGIS
RUN apt-get update && \
    apt-get install -y -qq software-properties-common && \
    add-apt-repository ppa:ubuntugis/ppa && \

RUN pip install gdal=="$(gdal-config --version).*"

RUN mkdir -p /code

WORKDIR /code

COPY requirements.txt /tmp/requirements.txt

RUN set -ex && \
    pip install --upgrade pip && \
    pip install -r /tmp/requirements.txt && \
    rm -rf /root/.cache/

COPY . /code/

RUN python manage.py collectstatic --noinput

EXPOSE 8000

# replace demo.wsgi with <project_name>.wsgi
CMD ["gunicorn", "--bind", ":8000", "--workers", "2", "optimap.wsgi"]
