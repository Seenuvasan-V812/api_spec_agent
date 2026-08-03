package com.example.bookstore.dto;

import com.fasterxml.jackson.annotation.JsonIgnore;
import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

import java.time.OffsetDateTime;
import java.util.List;

/** A book available in the store. */
public class Book {

    private Long id;

    @NotBlank
    @Size(max = 200)
    private String title;

    @JsonProperty("authorName")
    private String author;

    private Genre genre;

    private List<String> tags;

    private OffsetDateTime publishedAt;

    @JsonIgnore
    private String internalNote;
}
