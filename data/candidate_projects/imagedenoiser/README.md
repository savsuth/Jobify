# Image Denoising with GAN

## Introduction

Path tracing is a rendering technique widely used in high-end animation studios such as Pixar and DreamWorks to produce photorealistic frames. It operates by emitting thousands of Monte Carlo rays per pixel. These rays interact with objects in the scene through reflection, refraction, and absorption, and the returned colors are averaged to compute each pixel's final value. This approach produces accurate results but is computationally expensive; rendering a single frame can require 8 to 16 hours.

This project investigates a Generative Adversarial Network (GAN) approach to denoising as a method of reducing that rendering time. Rather than requiring tens of thousands of samples per pixel (32K spp), the renderer produces a noisy image using as few as 4 to 8 spp. The GAN then reconstructs the image into a high-quality, photorealistic result in a fraction of a second, reducing total rendering time from hours to seconds.

## Installation

**Requirements:**
- Python 3.10
- TensorFlow (v1.0 or v1.1)
- PIL
- [Checkpoint file](https://uofi.box.com/shared/static/21a5jwdiqpnx24c50cyolwzwycnr3fwe.gz)
- [Dataset](https://uofi.box.com/shared/static/gy0t3vgwtlk1933xbtz1zvhlakkdac3n.zip)

## Dataset

The training set consists of 40 images sampled from Pixar titles. Noise was synthesized by applying Gaussian perturbations across a 5x5 grid of standard deviation settings, comprising five sets of five sigma values each, producing 1,000 training sampl