package io.jenkins.plugins.cicvcd;

import hudson.EnvVars;
import hudson.Extension;
import hudson.FilePath;
import hudson.Launcher;
import hudson.model.AbstractProject;
import hudson.model.Run;
import hudson.model.TaskListener;
import hudson.tasks.BuildStepDescriptor;
import hudson.tasks.Builder;

import java.io.*;

import jenkins.tasks.SimpleBuildStep;
import org.jenkinsci.Symbol;
import org.kohsuke.stapler.DataBoundConstructor;

public class SDICI extends Builder implements SimpleBuildStep {

    private final String name;

    @DataBoundConstructor
    public SDICI(String name) {
        this.name = name;
    }

    public String getName() {
        return name;
    }

    @Override
    public void perform(Run<?, ?> run, FilePath workspace, EnvVars env, Launcher launcher, TaskListener listener)
            throws InterruptedException, IOException {

        boolean success = true;
        String fileName = "requirements.txt";
        File requirementsFile = new File(workspace.getRemote() + "/" + fileName);
        StringBuilder requirementsContent = new StringBuilder();

        if (requirementsFile.exists()) {
            try (BufferedReader br = new BufferedReader(new FileReader(requirementsFile))) {
                String line;
                while ((line = br.readLine()) != null) {
                    requirementsContent.append(line).append("\n");
                    listener.getLogger().println("Requirement: " + line);
                }
                listener.getLogger().println("Processed " + fileName + "successfully");
            } catch (IOException e) {
                e.printStackTrace();
                listener.getLogger().println("Failed to read " + fileName);
                success = false;
            }
        } else {
            listener.getLogger().println(fileName + " file does not exist.");
            success = false;
        }

        if (success) {
            String[] generatedConfigs = generateConfigs(requirementsContent.toString(), listener);
            success = outputGeneratedConfigFiles(workspace, listener, generatedConfigs);
        }

        if (success) {
            listener.getLogger().println("CI Phase successful");
        } else {
            listener.getLogger().println("CI Phase failed");
            throw new InterruptedException("CI failed");
        }
    }

    private String[] generateConfigs(String requirementsContent, TaskListener listener) throws InterruptedException {
        String loadingMessage = "Generating config files: [";
        listener.getLogger().print(loadingMessage);

        for (int i = 0; i < 5; i++) { // Simulate some processing time hh
            Thread.sleep(500);
            listener.getLogger().print("=");
        }
        listener.getLogger().println("] Done!");

        String config1 = "tasks:\n" + //
                        "  - task: image-upscale\n" + //
                        "    id: 0\n" + //
                        "    service: \"stabilityai/stable-diffusion-x4-upscaler\"\n" + //
                        "    dep: [-1]\n" + //
                        "    args:\n" + //
                        "      image: camera-capture-t0.jpg\n" + //
                        "    return:\n" + //
                        "      image: resource-0.jpg\n" + //
                        "  - task: object-detection\n" + //
                        "    id: 1\n" + //
                        "    service: \"facebook/detr-resnet-101\"\n" + //
                        "    dep: [0]\n" + //
                        "    args:\n" + //
                        "      image: resource-0.jpg\n" + //
                        "    return:\n" + //
                        "      image: resource-1.jpg\n" + //
                        "  - task: image-classification\n" + //
                        "    id: 2\n" + //
                        "    service: \"microsoft/resnet-50\"\n" + //
                        "    dep: [1]\n" + //
                        "    args:\n" + //
                        "      image: resource-1.jpg\n" + //
                        "    return:\n" + //
                        "      json: result-2.json";
        String config2 = "tasks:\n" + //
                        "  - task: image-upscale\n" + //
                        "    id: 0\n" + //
                        "    service: \"stabilityai/stable-diffusion-x4-upscaler\"\n" + //
                        "    dep: [-1]\n" + //
                        "    args:\n" + //
                        "      image: camera-capture-t0.jpg\n" + //
                        "    return:\n" + //
                        "      image: resource-0.jpg\n" + //
                        "  - task: image-denoise\n" + //
                        "    id: 1\n" + //
                        "    service: \"google/maxim-s3-denoising-sidd\"\n" + //
                        "    dep: [0]\n" + //
                        "    args:\n" + //
                        "      image: resource-0.jpg\n" + //
                        "    return:\n" + //
                        "      image: resource-1.jpg\n" + //
                        "  - task: image-segmentation\n" + //
                        "    id: 2\n" + //
                        "    service: \"nvidia/segformer-b0-finetuned-ade-512-512\"\n" + //
                        "    dep: [1]\n" + //
                        "    args:\n" + //
                        "      image: resource-1.jpg\n" + //
                        "    return:\n" + //
                        "      json: result-2.json";
        String config3 = "tasks:\n" + //
                        "  - task: image-variation\n" + //
                        "    id: 0\n" + //
                        "    service: \"lambdalabs/sd-image-variations-diffusers\"\n" + //
                        "    dep: [-1]\n" + //
                        "    args:\n" + //
                        "      image: camera-capture-t0.jpg\n" + //
                        "    return:\n" + //
                        "      image: resource-0.jpg\n" + //
                        "  - task: object-detection\n" + //
                        "    id: 1\n" + //
                        "    service: \"hustvl/yolos-tiny\"\n" + //
                        "    dep: [0]\n" + //
                        "    args:\n" + //
                        "      image: resource-0.jpg\n" + //
                        "    return:\n" + //
                        "      image: resource-1.jpg\n" + //
                        "  - task: image-classification\n" + //
                        "    id: 2\n" + //
                        "    service: \"microsoft/resnet-50\"\n" + //
                        "    dep: [1]\n" + //
                        "    args:\n" + //
                        "      image: resource-1.jpg\n" + //
                        "    return:\n" + //
                        "      json: result-2.json";
        String config4 = "tasks:\n" + //
                        "  - task: image-upscale\n" + //
                        "    id: 0\n" + //
                        "    service: \"stabilityai/stable-diffusion-x4-upscaler\"\n" + //
                        "    dep: [-1]\n" + //
                        "    args:\n" + //
                        "      image: camera-capture-t0.jpg\n" + //
                        "    return:\n" + //
                        "      image: resource-0.jpg\n" + //
                        "  - task: object-detection\n" + //
                        "    id: 1\n" + //
                        "    service: \"facebook/detr-resnet-101\"\n" + //
                        "    dep: [0]\n" + //
                        "    args:\n" + //
                        "      image: resource-0.jpg\n" + //
                        "    return:\n" + //
                        "      image: resource-1.jpg\n" + //
                        "  - task: image-denoise\n" + //
                        "    id: 2\n" + //
                        "    service: \"google/maxim-s3-deblurring-realblur-r\"\n" + //
                        "    dep: [1]\n" + //
                        "    args:\n" + //
                        "      image: resource-1.jpg\n" + //
                        "    return:\n" + //
                        "      image: resource-2.jpg\n" + //
                        "  - task: image-classification\n" + //
                        "    id: 3\n" + //
                        "    service: \"microsoft/resnet-18\"\n" + //
                        "    dep: [2]\n" + //
                        "    args:\n" + //
                        "      image: resource-2.jpg\n" + //
                        "    return:\n" + //
                        "      json: result-3.json";
        String config5 = "tasks:\n" + //
                        "  - task: image-denoise\n" + //
                        "    id: 0\n" + //
                        "    service: \"google/maxim-s3-denoising-sidd\"\n" + //
                        "    dep: [-1]\n" + //
                        "    args:\n" + //
                        "      image: camera-capture-t0.jpg\n" + //
                        "    return:\n" + //
                        "      image: resource-0.jpg\n" + //
                        "  - task: image-segmentation\n" + //
                        "    id: 1\n" + //
                        "    service: \"facebook/maskformer-swin-large-ade\"\n" + //
                        "    dep: [0]\n" + //
                        "    args:\n" + //
                        "      image: resource-0.jpg\n" + //
                        "    return:\n" + //
                        "      json: result-1.json";

        return new String[] { config1, config2, config3, config4, config5 };
    }

    private boolean outputGeneratedConfigFiles(FilePath workspace, TaskListener listener, String[] generatedConfigs)
            throws IOException {
        boolean success = true;
        for (int i = 0; i < generatedConfigs.length; i++) {
            String fileName = "ci_output" + (i + 1) + ".yaml";
            try {
                writeFile(workspace, fileName, generatedConfigs[i], listener);
            } catch (IOException e) {
                e.printStackTrace();
                listener.getLogger().println("Failed to generate " + fileName);
                success = false;
            }

        }
        return success;
    }

    private void writeFile(FilePath workspace, String fileName, String content, TaskListener listener)
            throws IOException {
        try (PrintWriter p = new PrintWriter(new FileOutputStream(workspace.getRemote() + "/" + fileName, false))) {
            p.println(content);
            listener.getLogger().println("Generated " + fileName);
        } catch (Exception e) {
            e.printStackTrace();
            listener.getLogger().println("Failed to generate " + fileName);
        }
    }

    @Symbol("greet")
    @Extension
    public static final class DescriptorImpl extends BuildStepDescriptor<Builder> {

        public boolean isApplicable(Class<? extends AbstractProject> aClass) {
            return true;
        }

        @Override
        public String getDisplayName() {
            return "SDI-CI";
        }
    }
}