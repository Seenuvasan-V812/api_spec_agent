package com.example.inventory;

public class ItemNotFoundException extends RuntimeException {
    public ItemNotFoundException(long id) {
        super("Item " + id + " not found");
    }
}
