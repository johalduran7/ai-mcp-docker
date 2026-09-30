FROM python:3.12-slim
 
# Install awscli + awsume (setuptools required for awsume-configure on Python 3.12+)
RUN pip install --no-cache-dir setuptools awscli awsume
 
# awsume needs a writable home for its cache; default root home is fine
ENV HOME=/root
 
# Bake in the shell alias/function awsume needs to export creds into the
# invoking shell, plus autocomplete. Both --shell and --autocomplete-file
# are required flags for awsume-configure.
RUN awsume-configure --shell bash \
    --autocomplete-file /root/.bash_completion \
    --alias-file /root/.bashrc
 
WORKDIR /work
 
# Start an interactive login shell so .bashrc (and the awsume alias) loads automatically
CMD ["/bin/bash", "-l"]
 
