import optimap

def get_version(request):
    """
    Return package version as listed in `__version__` in `init.py`.
    """
    return {"optimap_version": optimap.__version__}
