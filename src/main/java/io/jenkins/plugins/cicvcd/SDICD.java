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

import io.kubernetes.client.util.ClientBuilder;
import io.kubernetes.client.util.KubeConfig;
import jenkins.tasks.SimpleBuildStep;

import org.apache.commons.io.IOUtils;
import org.jenkinsci.Symbol;
import org.kohsuke.stapler.DataBoundConstructor;
import org.kohsuke.stapler.QueryParameter;
import org.json.JSONArray;
import org.json.JSONObject;
import org.json.JSONTokener;


import io.kubernetes.client.openapi.ApiClient;
import io.kubernetes.client.openapi.ApiException;
import io.kubernetes.client.openapi.Configuration;
import io.kubernetes.client.openapi.apis.CoreV1Api;
import io.kubernetes.client.openapi.models.V1Pod;
import io.kubernetes.client.openapi.models.V1PodList;
import io.kubernetes.client.util.Config;

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
        //String kubeConfigPath = "src/main/resources/kube.config";

        try (PrintWriter p = new PrintWriter(new FileOutputStream(workspace.getRemote()+"/Dockerfile", false)))
        {
            //ApiClient client = ClientBuilder.kubeconfig(KubeConfig.loadKubeConfig(new FileReader(kubeConfigPath))).build();
            //Configuration.setDefaultApiClient(client);

            //CoreV1Api api = new CoreV1Api();
            //V1PodList list =
            //       api.listPodForAllNamespaces(null, null, null, null, null, null, null, null, null,  null);
            //for (V1Pod item : list.getItems()) {
            //    System.out.println(item.getMetadata().getName());
            //}

            InputStream fis = new FileInputStream(workspace.getRemote()+"/CV_output.json");
            String jsonText = IOUtils.toString(fis);
            JSONArray jsonArray = new JSONArray(jsonText);

            listener.getLogger().println("Creating Docker files");
            for(int i = 0; i < jsonArray.length(); i++){
                JSONObject composition_json = (JSONObject) jsonArray.get(0);

                JSONArray composition = new JSONArray(composition_json.getJSONArray("composition"));
            }
            p.println("FROM ubuntu:22.04");
            p.println("ENV TERM linux");
            p.println("ENV DEBIAN_FRONTEND noninteractive");
            p.println("RUN apt-get update");
            p.println("CMD [\"/bin/bash\"]");
            listener.getLogger().println("Docker files generated");

            listener.getLogger().println("Creating docker-compose");
            PrintWriter p2 = new PrintWriter(new FileOutputStream(workspace.getRemote()+"/docker-compose.yml", false));
            p2.println("version: 3.9");
            p2.println();
            p2.println("services:");
            p2.println("  service1:");
            p2.println("    image: image1");
            p2.println("  service2:");
            p2.println("    image: image2");
            p2.println("  service3:");
            p2.println("    image: image3");
            p2.close();

            listener.getLogger().println("Docker-compose created");
            listener.getLogger().println("Deploying composition to Kubernetes Cluster");
            listener.getLogger().println("CD success");
        } catch(Exception e) {
            e.printStackTrace();
            listener.getLogger().println("CD failed");
            throw new InterruptedException("CD failed");
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
