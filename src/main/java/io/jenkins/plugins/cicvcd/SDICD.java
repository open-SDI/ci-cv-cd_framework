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
import hudson.util.FormValidation;

import java.io.*;
import javax.servlet.ServletException;
import jenkins.tasks.SimpleBuildStep;

import org.apache.commons.io.IOUtils;
import org.jenkinsci.Symbol;
import org.kohsuke.stapler.DataBoundConstructor;
import org.kohsuke.stapler.QueryParameter;
import org.json.JSONArray;
import org.json.JSONObject;
import org.json.JSONTokener;

public class SDICD extends Builder implements SimpleBuildStep {

    private final String name;


    @DataBoundConstructor
    public SDICD(String name) {
        this.name = name;
    }

    public String getName() {
        return name;
    }


    @Override
    public void perform(Run<?, ?> run, FilePath workspace, EnvVars env, Launcher launcher, TaskListener listener)
            throws InterruptedException, IOException {

        try {
            InputStream fis = new FileInputStream(workspace.getRemote()+"/result.json");
            String jsonText = IOUtils.toString(fis);
            JSONArray jsonArray = new JSONArray(jsonText);
            for(int i = 0; i < jsonArray.length(); i++){
                JSONObject composition = (JSONObject) jsonArray.get(0);
                listener.getLogger().println("Deploying composition " + (i+1));
            }

            listener.getLogger().println("CD success");
        } catch(Exception e) {
            e.printStackTrace();
            listener.getLogger().println("CD failed");
        }
    }

    @Symbol("greet")
    @Extension
    public static final class DescriptorImpl extends BuildStepDescriptor<Builder> {

        public FormValidation doCheckName(@QueryParameter String value, @QueryParameter boolean useFrench)
                throws IOException, ServletException {
            if (value.length() == 0)
                return FormValidation.error(Messages.HelloWorldBuilder_DescriptorImpl_errors_missingName());
            if (value.length() < 4)
                return FormValidation.warning(Messages.HelloWorldBuilder_DescriptorImpl_warnings_tooShort());
            if (!useFrench && value.matches(".*[éáàç].*")) {
                return FormValidation.warning(Messages.HelloWorldBuilder_DescriptorImpl_warnings_reallyFrench());
            }
            return FormValidation.ok();
        }

        @Override
        public boolean isApplicable(Class<? extends AbstractProject> aClass) {
            return true;
        }

        @Override
        public String getDisplayName() {
            return "SDI-CD";
        }
    }
}
