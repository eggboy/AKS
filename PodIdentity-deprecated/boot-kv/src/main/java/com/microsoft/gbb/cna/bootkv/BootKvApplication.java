package com.microsoft.gbb.cna.bootkv;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@SpringBootApplication
public class BootKvApplication {


	public static void main(String[] args) {
		SpringApplication.run(BootKvApplication.class, args);
	}

}

@RestController
class KVController {
	@Value("${connectionString}")
	private String connectionString;

	@GetMapping("/getkey")
	public String getKey() {
		return connectionString;
	}
}