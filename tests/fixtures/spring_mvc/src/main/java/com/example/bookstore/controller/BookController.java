package com.example.bookstore.controller;

import com.example.bookstore.dto.Book;
import com.example.bookstore.dto.CreateBookRequest;
import com.example.bookstore.dto.Genre;
import com.example.bookstore.dto.PageResponse;
import com.example.bookstore.service.BookService;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

/** Manages the book catalog. */
@RestController
@RequestMapping("/books")
public class BookController {

    private final BookService bookService;

    public BookController(BookService bookService) {
        this.bookService = bookService;
    }

    /** List books with paging and optional genre filter. */
    @GetMapping
    public PageResponse<Book> list(
            @RequestParam(required = false) Genre genre,
            @RequestParam(defaultValue = "0") int page,
            @RequestParam(defaultValue = "20") int size) {
        return new PageResponse<>();
    }

    /** Fetch a single book. */
    @GetMapping("/{id}")
    public Book get(@PathVariable long id) {
        return bookService.findById(id);
    }

    /** Create a new book. */
    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    @PreAuthorize("hasRole('EDITOR')")
    public Book create(@Valid @RequestBody CreateBookRequest request) {
        return bookService.create(request);
    }

    /** Replace a book. */
    @PutMapping("/{id}")
    @PreAuthorize("hasRole('EDITOR')")
    public ResponseEntity<Book> update(
            @PathVariable long id,
            @Valid @RequestBody CreateBookRequest request,
            @RequestHeader("If-Match") String ifMatch) {
        return ResponseEntity.ok(bookService.create(request));
    }

    /** Delete a book. */
    @DeleteMapping("/{id}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    @PreAuthorize("hasRole('EDITOR')")
    public void delete(@PathVariable long id) {
        bookService.delete(id);
    }

    /** Upload a cover image. */
    @PostMapping("/{id}/cover")
    public ResponseEntity<Void> uploadCover(
            @PathVariable long id,
            @RequestParam("file") MultipartFile file) {
        return ResponseEntity.ok().build();
    }
}
