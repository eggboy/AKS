package com.example.echo_app;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@SpringBootApplication
@RestController
public class EchoAppApplication {

	@Value("${message}")
	private String message;

	public static void main(String[] args) {
		SpringApplication.run(EchoAppApplication.class, args);
	}

	@GetMapping
	public String echo() {
		return message + " App!";
	}

}
