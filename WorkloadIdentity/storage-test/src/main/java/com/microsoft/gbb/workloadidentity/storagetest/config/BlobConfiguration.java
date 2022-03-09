package com.microsoft.gbb.workloadidentity.storagetest.config;

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

	@Value("${BLOB_ACCOUNT_NAME}")
	private String BLOB_ACCOUNT_NAME;

	@Value("${BLOB_CONTAINER_NAME}")
	private String BLOB_CONTAINER_NAME;

	private FederatedCredential federatedCredential;

	public BlobConfiguration(FederatedCredential federatedCredential) {
		this.federatedCredential = federatedCredential;
	}

	@Bean
	public BlobContainerClient blobContainerClient() {

		String endpoint = String.format(Locale.ROOT, "https://%s.blob.core.windows.net", BLOB_ACCOUNT_NAME);

		BlobServiceClient storageClient = new BlobServiceClientBuilder().endpoint(endpoint)
				.credential(federatedCredential).buildClient();

		return storageClient.getBlobContainerClient(BLOB_CONTAINER_NAME);
	}

}
