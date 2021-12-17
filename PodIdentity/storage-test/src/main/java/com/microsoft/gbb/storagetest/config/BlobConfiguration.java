package com.microsoft.gbb.storagetest.config;

import com.azure.identity.ManagedIdentityCredential;
import com.azure.identity.ManagedIdentityCredentialBuilder;
import com.azure.identity.UsernamePasswordCredential;
import com.azure.identity.UsernamePasswordCredentialBuilder;
import com.azure.storage.blob.BlobContainerClient;
import com.azure.storage.blob.BlobServiceClient;
import com.azure.storage.blob.BlobServiceClientBuilder;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.util.Locale;

@Slf4j
@Configuration
public class BlobConfiguration {

	@Value("${azure.storage.accountName}")
	String accountName;

	@Bean
	public BlobContainerClient blobContainerClient() {
		String clientId = System.getenv("AZURE_CLIENT_ID");
		String containerName = System.getenv("BLOB_CONTAINER_NAME");
		String username = System.getenv("USERNAME");
		String password = System.getenv("PASSWORD");;

		// ManagedIdentityCredential managedIdentityCredential = new
		// ManagedIdentityCredentialBuilder()
		// .clientId(clientId)
		// .build();

		String endpoint = String.format(Locale.ROOT, "https://%s.blob.core.windows.net", accountName);

		UsernamePasswordCredential managedIdentityCredential = new UsernamePasswordCredentialBuilder().clientId(clientId)
				.username(username).password(password).build();

		BlobServiceClient storageClient = new BlobServiceClientBuilder().endpoint(endpoint)
				.credential(managedIdentityCredential).buildClient();
		return storageClient.getBlobContainerClient(containerName);
	}

}