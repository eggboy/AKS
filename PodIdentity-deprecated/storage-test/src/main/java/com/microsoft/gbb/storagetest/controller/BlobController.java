package com.microsoft.gbb.storagetest.controller;

import com.azure.storage.blob.BlobContainerClient;
import com.azure.storage.blob.models.BlobItem;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class BlobController {
    private BlobContainerClient containerClient;

    public BlobController(BlobContainerClient blobContainerClient) {
        this.containerClient = blobContainerClient;
    }

    @GetMapping("/bloblist")
    public String readBlobFile() {
        StringBuilder blobList = new StringBuilder();

        for (BlobItem blobItem : containerClient.listBlobs()) {
            blobList.append(blobItem.getName() + " ");
        }

        return blobList.toString();
    }

}
