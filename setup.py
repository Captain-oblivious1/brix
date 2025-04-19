from setuptools import setup, find_packages

setup(
    name="brix",
    version="0.1",
    packages=find_packages(),
    install_requires=["networkx"],  # Minimal dependencies
    description="A Python-based build system using modular building blocks",
    author="Your Name",
    author_email="your.email@example.com",
)

