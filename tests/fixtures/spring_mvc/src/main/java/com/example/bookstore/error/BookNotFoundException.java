package com.example.bookstore.error;

public class BookNotFoundException extends RuntimeException {
    public BookNotFoundException(long id) {
        super("Book " + id + " not found");
    }
}
