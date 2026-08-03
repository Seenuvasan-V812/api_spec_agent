package com.example.bookstore.service;

import com.example.bookstore.dto.Book;
import com.example.bookstore.dto.CreateBookRequest;
import com.example.bookstore.error.BookNotFoundException;
import org.springframework.stereotype.Service;

@Service
public class BookService {

    public Book findById(long id) {
        throw new BookNotFoundException(id);
    }

    public Book create(CreateBookRequest request) {
        return new Book();
    }

    public void delete(long id) {
        throw new BookNotFoundException(id);
    }
}
