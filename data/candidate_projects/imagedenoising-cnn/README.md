# ImageDenoising
Images captured in real-world environments often contain significant noise, which can adversely impact low-level vision tasks and related applications. Effective noise removal is therefore a critical step in enhancing image quality and enabling robust computer vision performance.

Over the years many techniques and filters have been introduced for image denoising. They used to work to some extent in denoising the images. But most of these techniques assumed the noise in images to be gaussian noise or impulse noise. But this assumption doesn't completely hold true for real noise in photographs. The real world noise is more sophisticated and diverse. Due to this most of these techniques performed poorly in completely removing real noise from images.

This is where deep learning comes into picture and experiments have proved that training a convolutional blind denoising deep learning network outperforms other techniques in image denoising tasks by a large margin.

In this case study, I have implemented four state-of-the-art CNN based architecture for image denoising tasks as follows
1. Autoencoder (as a baseline model)
2. CBDNet
3. PRIDNet
4. RIDNet

Among these models, RIDNet gave the best performance and using it.

Below are the few predcitions of RIDNet model in image denoising on some real noisy images
![image](https://user-images.githubusercontent.com/85414148/131446414-a4dbe8cf-f8c6-4ec5-887a-fc3a4f3deb42.png)
![image](https://user-images.githubusercontent