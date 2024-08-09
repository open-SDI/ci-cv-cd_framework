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

public class SDICV extends Builder implements SimpleBuildStep {

    private final String name;

    @DataBoundConstructor
    public SDICV(String name) {
        this.name = name;
    }

    public String getName() {
        return name;
    }

    public class Composition {
        public Component[] components;

        public Composition(int size){
            this.components = new Component[size];
        }

        public void addComponent(int id, Component c) {
            this.components[id] = c;
        }
    }

    public class Component {
        public String task;
        public String service;
        public int[] dep;
        public String[] args;
        public String[] returns;

        public Component(String task, String service, int[] dep, String[] args, String[] returns) {
            this.task = task;
            this.service = service;
            this.dep = dep;
            this.args = args;
            this.returns = returns;
        }
    }


    @Override
    public void perform(Run<?, ?> run, FilePath workspace, EnvVars env, Launcher launcher, TaskListener listener)
            throws InterruptedException, IOException {
        FileOutputStream fos = null;
        boolean success = true;

        // TODO: Change it to multi file input
        String fileName = "ci_output1.yaml";
        File compositionFile = new File(workspace.getRemote() + "/" + fileName);
        Composition composition;

        if (compositionFile.exists()) {
            composition = readComposition(fileName);

            String testResult = testComposition(composition);
            success = printResultFile(workspace, listener, testResult);

        } else {
            listener.getLogger().println(fileName + " file does not exist.");
            success = false;
        }

        if (success) {
            listener.getLogger().println("CV Phase successful");
        } else {
            listener.getLogger().println("CV Phase failed");
        }

    }

    public Composition readComposition(String fileName) {
        // TODO: Reading Yaml File
        Composition composition = new Composition(3);

        Component component = new Component(
                "image-upscale",
                "stabilityai/stable-diffusion-x4-upscaler",
                new int[]{-1},
                new String[]{"image: camera-capture-t0.jpg"},
                new String[]{"image: resource-0.jpg"}
        );
        composition.addComponent(0, component);

        component = new Component(
                "image-upscale",
                "facebook/detr-resnet-101",
                new int[]{-1},
                new String[]{"image: resource-0.jpg"},
                new String[]{"image: resource-1.jpg"}
        );
        composition.addComponent(1, component);

        component = new Component(
                "image-classification",
                "microsoft/resnet-50",
                new int[]{1},
                new String[]{"image: resource-1.jpg"},
                new String[]{"json: actions.json"}
        );
        composition.addComponent(2, component);

        return composition;
    }

    private String testComposition(Composition composition) {
        // TODO: Test Composition by Simulation
        return "[\n" +
                "\t{\n" +
                "\t\t\"composition\":[\n" +
                "\t\t\t{\n" +
                "\t\t\t\t\"task\":\"image-upscale\",\n" +
                "\t\t\t\t\"service\":\"stabilityai/stable-diffusion-x4-upscaler\",\n" +
                "\t\t\t\t\"dep\":[\n" +
                "\t\t\t\t\t-1\n" +
                "\t\t\t\t],\n" +
                "\t\t\t\t\"args\":{\n" +
                "\t\t\t\t\t\"image\":\"camera-capture-t0.jpg\"\n" +
                "\t\t\t\t},\n" +
                "\t\t\t\t\"return\":{\n" +
                "\t\t\t\t\t\"image\":\"resource-0.jpg\"\n" +
                "\t\t\t\t}\n" +
                "\t\t\t},\n" +
                "\t\t\t{\n" +
                "\t\t\t\t\"task\":\"object-detection\",\n" +
                "\t\t\t\t\"service\":\"stabilityai/stable-diffusion-x4-upscaler\",\n" +
                "\t\t\t\t\"dep\":[\n" +
                "\t\t\t\t\t0\n" +
                "\t\t\t\t],\n" +
                "\t\t\t\t\"args\":{\n" +
                "\t\t\t\t\t\"image\":\"resource-0.jpg\"\n" +
                "\t\t\t\t},\n" +
                "\t\t\t\t\"return\":{\n" +
                "\t\t\t\t\t\"image\":\"resource-1.jpg\"\n" +
                "\t\t\t\t}\n" +
                "\t\t\t},\n" +
                "\t\t\t{\n" +
                "\t\t\t\t\"task\":\"image-classification\",\n" +
                "\t\t\t\t\"service\":\"microsoft/resnet-50\",\n" +
                "\t\t\t\t\"dep\":[\n" +
                "\t\t\t\t\t1\n" +
                "\t\t\t\t],\n" +
                "\t\t\t\t\"args\":{\n" +
                "\t\t\t\t\t\"image\":\"resource-1.jpg\"\n" +
                "\t\t\t\t},\n" +
                "\t\t\t\t\"return\":{\n" +
                "\t\t\t\t\t\"json\":\"actions.json\"\n" +
                "\t\t\t\t}\n" +
                "\t\t\t}\n" +
                "\t\t],\n" +
                "\t\t\"is-valid-environment\":\"True\",\n" +
                "\t\t\"operational-domain-90\":{\n" +
                "\t\t\t\"sun-angle\":{\n" +
                "\t\t\t\t\"min\":-10,\n" +
                "\t\t\t\t\"max\":180\n" +
                "\t\t\t},\n" +
                "\t\t\t\"precipitation\":{\n" +
                "\t\t\t\t\"min\":0,\n" +
                "\t\t\t\t\"max\":50\n" +
                "\t\t\t}\n" +
                "\t\t},\n" +
                "\t\t\"resource-profile\":{\n" +
                "\t\t\t\"bandwidth-usage\":{\n" +
                "\t\t\t\t\"min\":\"100Mbps\",\n" +
                "\t\t\t\t\"max\":\"1Gbps\",\n" +
                "\t\t\t\t\"median\":\"500Mbps\"\n" +
                "\t\t\t},\n" +
                "\t\t\t\"battery-consumption\":{\n" +
                "\t\t\t\t\"min\":\"10W\",\n" +
                "\t\t\t\t\"max\":\"20W\",\n" +
                "\t\t\t\t\"median\":\"15W\"\n" +
                "\t\t\t},\n" +
                "\t\t\t\"memory-usage\":{\n" +
                "\t\t\t\t\"min\":\"1GB\",\n" +
                "\t\t\t\t\"max\":\"2GB\",\n" +
                "\t\t\t\t\"median\":\"1.5GB\"\n" +
                "\t\t\t}\n" +
                "\t\t}\n" +
                "\t},\n" +
                "\t{\n" +
                "\t\t\"composition\":[\n" +
                "\t\t\t{\n" +
                "\t\t\t\t\"task\":\"image-upscale\",\n" +
                "\t\t\t\t\"service\":\"stabilityai/stable-diffusion-x4-upscaler\",\n" +
                "\t\t\t\t\"dep\":[\n" +
                "\t\t\t\t\t-1\n" +
                "\t\t\t\t],\n" +
                "\t\t\t\t\"args\":{\n" +
                "\t\t\t\t\t\"image\":\"camera-capture-t0.jpg\"\n" +
                "\t\t\t\t},\n" +
                "\t\t\t\t\"return\":{\n" +
                "\t\t\t\t\t\"image\":\"resource-0.jpg\"\n" +
                "\t\t\t\t}\n" +
                "\t\t\t},\n" +
                "\t\t\t{\n" +
                "\t\t\t\t\"task\":\"image-denoise\",\n" +
                "\t\t\t\t\"service\":\"google/maxim-s3-denoising-sidd\",\n" +
                "\t\t\t\t\"dep\":[\n" +
                "\t\t\t\t\t0\n" +
                "\t\t\t\t],\n" +
                "\t\t\t\t\"args\":{\n" +
                "\t\t\t\t\t\"image\":\"resource-0.jpg\"\n" +
                "\t\t\t\t},\n" +
                "\t\t\t\t\"return\":{\n" +
                "\t\t\t\t\t\"image\":\"resource-1.jpg\"\n" +
                "\t\t\t\t}\n" +
                "\t\t\t},\n" +
                "\t\t\t{\n" +
                "\t\t\t\t\"task\":\"image-segmentation\",\n" +
                "\t\t\t\t\"service\":\"nvidia/segformer-b0-finetuned-ade-512-512\",\n" +
                "\t\t\t\t\"dep\":[\n" +
                "\t\t\t\t\t1\n" +
                "\t\t\t\t],\n" +
                "\t\t\t\t\"args\":{\n" +
                "\t\t\t\t\t\"image\":\"resource-1.jpg\"\n" +
                "\t\t\t\t},\n" +
                "\t\t\t\t\"return\":{\n" +
                "\t\t\t\t\t\"json\":\"actions.json\"\n" +
                "\t\t\t\t}\n" +
                "\t\t\t}\n" +
                "\t\t],\n" +
                "\t\t\"is-valid-environment\":\"True\",\n" +
                "\t\t\"operational-domain-90\":{\n" +
                "\t\t\t\"sun-angle\":{\n" +
                "\t\t\t\t\"min\":-180,\n" +
                "\t\t\t\t\"max\":180\n" +
                "\t\t\t},\n" +
                "\t\t\t\"precipitation\":{\n" +
                "\t\t\t\t\"min\":0,\n" +
                "\t\t\t\t\"max\":100\n" +
                "\t\t\t}\n" +
                "\t\t},\n" +
                "\t\t\"resource-profile\":{\n" +
                "\t\t\t\"bandwidth-usage\":{\n" +
                "\t\t\t\t\"min\":\"1Gbps\",\n" +
                "\t\t\t\t\"max\":\"10Gbps\",\n" +
                "\t\t\t\t\"median\":\"5Gbps\"\n" +
                "\t\t\t},\n" +
                "\t\t\t\"battery-consumption\":{\n" +
                "\t\t\t\t\"min\":\"100W\",\n" +
                "\t\t\t\t\"max\":\"200W\",\n" +
                "\t\t\t\t\"median\":\"150W\"\n" +
                "\t\t\t},\n" +
                "\t\t\t\"memory-usage\":{\n" +
                "\t\t\t\t\"min\":\"10GB\",\n" +
                "\t\t\t\t\"max\":\"20GB\",\n" +
                "\t\t\t\t\"median\":\"15GB\"\n" +
                "\t\t\t}\n" +
                "\t\t}\n" +
                "\t}\n" +
                "]";
    }

    private boolean printResultFile(FilePath workspace, TaskListener listener, String testResult)
            throws IOException {
        boolean success = true;
        String fileName = "cv_output.json";
        try {
            writeFile(workspace, fileName, testResult, listener);
        } catch (IOException e) {
            e.printStackTrace();
            listener.getLogger().println("Failed to generate " + fileName);
            success = false;
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

        @Override
        public boolean isApplicable(Class<? extends AbstractProject> aClass) {
            return true;
        }

        @Override
        public String getDisplayName() {
            return "SDI-CV";
        }
    }
}
