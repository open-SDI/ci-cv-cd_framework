# Joe

Joe is not a real person. `joe.png` is Wikimedia Commons [File:GAN_Mensch_StyleGAN2.png](https://commons.wikimedia.org/wiki/File:GAN_Mensch_StyleGAN2.png), a StyleGAN2 output from thispersondoesnotexist.com. Commons marks it public domain because an algorithm made it.

The 1024x1024 original (Commons sha1 `3815c50de5a94886f277d07ace11a4314ac215a5`) was resized to 256x256 with OpenCV 4.6 `cv2.resize(..., (256, 256), interpolation=cv2.INTER_AREA)`, with no crop. YuNet 2022mar boxes his face 165.6 px wide in it, 0.6468 of the width, which sets the poster side in [scenario.yaml](../scenario.yaml).

`poster.obj` is a unit quad written for this scenario.
