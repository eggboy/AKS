package com.microsoft.gbb.storagetest.config;

import com.azure.identity.ManagedIdentityCredential;
import com.azure.identity.ManagedIdentityCredentialBuilder;
import com.azure.storage.blob.BlobContainerClient;
import com.azure.storage.blob.BlobServiceClient;
import com.azure.storage.blob.BlobServiceClientBuilder;
import lombok.extern.slf4j.Slf4j;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.util.Locale;

@Slf4j
@Configuration
public class BlobConfiguration {

    @Bean
    public BlobContainerClient blobContainerClient() {
        String msiClientId = System.getenv("AZURE_CLIENT_ID");
        String accountName = System.getenv("BLOB_ACCOUNT_NAME");
        String containerName = System.getenv("BLOB_CONTAINER_NAME");

        ManagedIdentityCredential managedIdentityCredential = new ManagedIdentityCredentialBuilder()
                .clientId(msiClientId)
                .build();

        String endpoint = String.format(Locale.ROOT, "https://%s.blob.core.windows.net", accountName);

        BlobServiceClient storageClient = new BlobServiceClientBuilder()
                .endpoint(endpoint)
                .credential(managedIdentityCredential)
                .buildClient();

        return storageClient.getBlobContainerClient(containerName);
    }
}