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
        FileOutputStream fos = null;

        try (PrintWriter p = new PrintWriter(new FileOutputStream(workspace.getRemote()+"/ci_output.yaml", false))){
            p.println("Test");
            listener.getLogger().println("CI success");
        } catch(Exception e){
            e.printStackTrace();
            listener.getLogger().println("CI failed");
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
