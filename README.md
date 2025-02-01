# Collab Render Test

This notebook is designed for beginners who want to use Blender in Google Colab without the need for expensive GPU resources. It provides a step-by-step guide to setting up Blender, rendering images, and performing bulk rendering of animations.

## Summary of the Code

1. **Connecting to Google Drive**:
   - The notebook connects to your Google Drive, allowing you to store and access your Blender projects directly from Colab.

2. **Installing Blender**:
   - The code downloads a specified version of Blender from the official repository and copies it to your Google Drive. This step should only be run once unless you are changing the Blender version.

3. **Normal Rendering of a Single Image**:
   - This section allows you to render a single image from a Blender project. You need to upload your project to the created Blender folder in your Google Drive before running this code.

4. **Bulk Rendering (Animation)**:
   - This section allows you to render animations. You specify the start and end frames, and the output settings. Ensure your project is in the Blender folder before running this code.

## Steps to Use the Notebook

1. **Run the Code to Connect to Google Drive**:
   - Execute the code to mount your Google Drive.

2. **Install Blender**:
   - Run the installation code to download and set up Blender. This should only be done once unless you are changing the version.

3. **Render a Single Image**:
   - Ensure your Blender project is uploaded to the designated folder in Google Drive. Execute the rendering code to generate a single image.

4. **Render an Animation**:
   - Make sure your project is in the Blender folder. Specify the start and end frames, then run the animation rendering code.

## Conclusion
This notebook provides a simple way to use Blender in Google Colab for rendering tasks. Feel free to modify the code as needed for your projects.
