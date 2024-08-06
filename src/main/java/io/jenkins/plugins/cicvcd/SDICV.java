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

import java.io.FileOutputStream;
import java.io.IOException;
import java.io.PrintWriter;

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


    @Override
    public void perform(Run<?, ?> run, FilePath workspace, EnvVars env, Launcher launcher, TaskListener listener)
            throws InterruptedException, IOException {
        FileOutputStream fos = null;

        try (PrintWriter p = new PrintWriter(new FileOutputStream(workspace.getRemote()+"/CV_output.json", false))){
            p.println("Test");
            listener.getLogger().println("CV success");
        } catch(Exception e) {
            e.printStackTrace();
            listener.getLogger().println("CV failed");
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
