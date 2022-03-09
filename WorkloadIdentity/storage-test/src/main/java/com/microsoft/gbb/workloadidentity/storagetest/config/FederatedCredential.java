package com.microsoft.gbb.workloadidentity.storagetest.config;

import com.azure.core.credential.AccessToken;
import com.azure.core.credential.TokenCredential;
import com.azure.core.credential.TokenRequestContext;
import com.microsoft.aad.msal4j.*;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Mono;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.time.ZoneOffset;
import java.util.Set;
import java.util.stream.Collectors;

@Slf4j
@Component
public class FederatedCredential implements TokenCredential {

	@Value("${AZURE_FEDERATED_TOKEN_FILE}")
	private String AZURE_FEDERATED_TOKEN_FILE;

	@Value("${AZURE_AUTHORITY_HOST}")
	private String AZURE_AUTHORITY_HOST;

	@Value("${AZURE_TENANT_ID}")
	private String AZURE_TENANT_ID;

	@Value("${AZURE_CLIENT_ID}")
	private String AZURE_CLIENT_ID;

	@Override
	public Mono<AccessToken> getToken(TokenRequestContext tokenRequestContext) {

		String clientAssertion = null;
		try {
			clientAssertion = Files.readString(Paths.get(AZURE_FEDERATED_TOKEN_FILE));
		}
		catch (IOException e) {
			log.error("Error getting AZURE_FEDERATED_TOKEN_FILE", e);
		}

		IClientCredential credential = ClientCredentialFactory.createFromClientAssertion(clientAssertion);

		StringBuilder authority = new StringBuilder();
		authority.append(AZURE_AUTHORITY_HOST);
		authority.append(AZURE_TENANT_ID);

		try {
			ConfidentialClientApplication app = ConfidentialClientApplication.builder(AZURE_CLIENT_ID, credential)
					.authority(authority.toString()).build();

			Set<String> scopes = tokenRequestContext.getScopes().stream().collect(Collectors.toSet());

			ClientCredentialParameters parameters = ClientCredentialParameters.builder(scopes).build();
			IAuthenticationResult result = app.acquireToken(parameters).join();

			return Mono.just(
					new AccessToken(result.accessToken(), result.expiresOnDate().toInstant().atOffset(ZoneOffset.UTC)));
		}
		catch (Exception e) {
			log.error("Error creating client application.", e);
		}

		return Mono.empty();
	}

}
