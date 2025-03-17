ARG UBUNTU_VERSION=22.04
FROM ubuntu:${UBUNTU_VERSION}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Europe/Berlin

# Install system dependencies for Python and GDAL
RUN apt-get update && \
    apt-get install -y -qq software-properties-common python-is-python3 python3-pip

# Add UbuntuGIS PPA for GDAL packages
RUN add-apt-repository ppa:ubuntugis/ppa && \
    apt-get update && \
    apt-get install -y -qq gdal-bin libgdal-dev python3-gdal

# Create and set the working directory
RUN mkdir -p /code
WORKDIR /code

# Copy your requirements first (for layer caching)
COPY requirements.txt /tmp/requirements.txt

# Install Python dependencies
RUN pip install --upgrade pip && \
    pip install -r /tmp/requirements.txt && \
    rm -rf /root/.cache

# Copy the rest of your code into the image
COPY . /code/

# Collect static files (only if your Django settings expect it)
RUN python manage.py collectstatic --noinput

EXPOSE 8000

# Launch Gunicorn (replace "optimap.wsgi" with your actual WSGI module if needed)
CMD ["gunicorn", "--bind", ":8000", "--workers", "2", "optimap.wsgi"]
# The above format is what I am currently testing. However, it does seem to causeway an issue with the tcp iot when im activating the container
# The script below provided a better output. However, I feel that it's not able to correctly link the GDAL to docker. 


# Use Python 3.10 base image instead of raw Ubuntu
#FROM python:3.10-slim-buster
#ENV PYTHONDONTWRITEBYTECODE=1 \
 #   PYTHONUNBUFFERED=1 \
 #   OPTIMAP_DEBUG=False \
 #   OPTIMAP_ALLOWED_HOST=* \
 #   DEBIAN_FRONTEND=noninteractive \
 #   TZ=Europe/Berlin

# Install system dependencies
#RUN apt-get update && apt-get install -y --no-install-recommends \
    # Required for GIS/spatial databases
#    gdal-bin \
#    libgdal-dev \
#    python3-gdal \
    # Required for Python build dependencies
#    gcc \
#    python3-dev \
#    # Postgres client libraries
#    libpq-dev \
#    && rm -rf /var/lib/apt/lists/*

#WORKDIR /code

# Install Python dependencies first (layer caching optimization)
#COPY requirements.txt /tmp/requirements.txt
#RUN pip install --upgrade pip && \
#    pip install -r /tmp/requirements.txt && \
#    rm -rf /root/.cache/pip

# Copy application code
#COPY . /code/

# Collect static files
#RUN python manage.py collectstatic --noinput

#EXPOSE 8000

#CMD ["gunicorn", "--bind", ":8000", "--workers", "2", "optimap.wsgi"]